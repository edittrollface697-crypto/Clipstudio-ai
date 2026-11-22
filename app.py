import glob, json, os, re, shutil, subprocess, zipfile
import gradio as gr
from google import genai

WORK_DIR = "/tmp/clipstudio_cloud"
os.makedirs(WORK_DIR, exist_ok=True)
MAX_CLIPS = 5

def fmt_mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"

def parse_vtt_to_segments(vtt_path):
    """Lightweight VTT parser so we don't need heavy Whisper RAM."""
    if not vtt_path or not os.path.exists(vtt_path):
        return []
    with open(vtt_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    pattern = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3}).*?\n(.*?)(?=\n\n|\Z)", re.S)
    segs = []
    for m in pattern.finditer(text):
        def to_sec(ts):
            h, mi, s = ts.split(":")
            return int(h) * 3600 + int(mi) * 60 + float(s)
        clean = re.sub(r"<[^>]+>", "", m.group(3)).replace("\n", " ").strip()
        if clean:
            segs.append({"start": to_sec(m.group(1)), "end": to_sec(m.group(2)), "text": clean})
    return segs

def get_video_filter(aspect_ratio, pan_x, srt_path=None):
    pan_ratio = (pan_x + 50) / 100.0
    if aspect_ratio == "9:16 (Reels / Shorts)":
        vf = f"crop=ih*9/16:ih:(iw-ih*9/16)*{pan_ratio:.2f}:0,scale=720:1280:flags=lanczos"
    elif aspect_ratio == "1:1 (Square)":
        vf = f"crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))*{pan_ratio:.2f}:0,scale=720:720:flags=lanczos"
    else:
        vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2"
    if srt_path and os.path.exists(srt_path):
        vf += f",subtitles={srt_path}:force_style='FontName=DejaVu Sans,FontSize=20,Bold=1,PrimaryColour=&H0000EAFF,OutlineColour=&H00000000,Outline=2,Alignment=2,MarginV=40'"
    return vf

def render_ffmpeg(input_mp4, output_mp4, vf, start_sec=None, end_sec=None):
    cmd = ["ffmpeg", "-y"]
    if start_sec is not None:
        cmd += ["-ss", f"{start_sec:.2f}"]
    cmd += ["-i", input_mp4]
    if start_sec is not None and end_sec is not None:
        cmd += ["-t", f"{max(1.0, end_sec - start_sec):.2f}"]
    cmd += [
        "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        output_mp4,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)

def find_viral_clips(segs, total_dur, num_clips, api_key):
    if api_key and api_key.strip() and segs:
        try:
            client = genai.Client(api_key=api_key.strip())
            transcript = "\n".join([f"[{s['start']:.1f}s-{s['end']:.1f}s] {s['text']}" for s in segs])[:25000]
            prompt = f"Pick the top {num_clips} viral 20-60s clips from this transcript (total {total_dur:.0f}s). Return ONLY a JSON array: [{{'title':'...','start':10,'end':45,'score':95,'reason':'...'}}]\n\n{transcript}"
            resp = client.models.generate_content(model="gemini-3.5-flash-lite", contents=prompt)
            raw = re.sub(r"^```(?:json)?|```$", "", resp.text.strip(), flags=re.M).strip()
            clips = json.loads(raw)
            if clips:
                return clips[:num_clips], "Gemini AI"
        except Exception:
            pass

    clip_len = min(35.0, max(15.0, total_dur / max(1, num_clips)))
    step = max(clip_len, (total_dur - clip_len) / max(1, num_clips - 1)) if num_clips > 1 else 0
    return [{"title": f"Highlight #{i+1}", "start": i * step, "end": min(total_dur, i * step + clip_len), "score": 88, "reason": "Auto-split highlight"} for i in range(num_clips)], "Auto-Segmenter"

def process_video(yt_url, api_key, num_clips, aspect_ratio, pan_x, render_full, burn_subs):
    empty_l = [gr.update(visible=False, value="")] * MAX_CLIPS
    empty_v = [gr.update(visible=False, value=None)] * MAX_CLIPS
    empty_f = [gr.update(visible=False, value=None)] * MAX_CLIPS
    if not (yt_url or "").strip():
        return ("❌ Paste a YouTube or Livestream URL.", None, None, None, *empty_l, *empty_v, *empty_f)

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    raw_mp4 = os.path.join(WORK_DIR, "raw.mp4")

    # Download video + audio + YouTube auto-subtitles
    dl = subprocess.run([
        "yt-dlp", "--js-runtimes", "deno", "--remote-components", "ejs:npm",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "--write-auto-sub", "--write-sub", "--sub-lang", "en,hi", "--sub-format", "vtt",
        "-o", raw_mp4, yt_url.strip()
    ], capture_output=True, text=True)

    if dl.returncode != 0 or not os.path.exists(raw_mp4):
        return (f"❌ Download failed:\n{dl.stderr[-400:]}", None, None, None, *empty_l, *empty_v, *empty_f)

    # Probe duration
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", raw_mp4], capture_output=True, text=True)
    total_dur = float(probe.stdout.strip() or 60.0)

    vtts = glob.glob(os.path.join(WORK_DIR, "*.vtt"))
    vtt_file = vtts[0] if vtts else None
    segs = parse_vtt_to_segments(vtt_file)
    clips_meta, engine = find_viral_clips(segs, total_dur, int(num_clips), api_key)

    full_out = None
    if render_full:
        out_p = os.path.join(WORK_DIR, "Full_Video_With_Audio.mp4")
        vf = get_video_filter(aspect_ratio, pan_x, vtt_file if burn_subs else None)
        if render_ffmpeg(raw_mp4, out_p, vf).returncode == 0:
            full_out = out_p

    labels, videos, files, rendered = [], [], [], []
    for i in range(MAX_CLIPS):
        if i < len(clips_meta):
            c = clips_meta[i]
            out_c = os.path.join(WORK_DIR, f"Clip_{i+1}.mp4")
            vf = get_video_filter(aspect_ratio, pan_x, None)
            if render_ffmpeg(raw_mp4, out_c, vf, c["start"], c["end"]).returncode == 0:
                rendered.append(out_c)
                labels.append(gr.update(visible=True, value=f"#### 🔥 Clip {i+1}: {c['title']} ({fmt_mmss(c['start'])}–{fmt_mmss(c['end'])})"))
                videos.append(gr.update(visible=True, value=out_c))
                files.append(gr.update(visible=True, value=out_c))
                continue
        labels.append(gr.update(visible=False, value=""))
        videos.append(gr.update(visible=False, value=None))
        files.append(gr.update(visible=False, value=None))

    zip_p = None
    if rendered:
        zip_p = os.path.join(WORK_DIR, "All_Clips.zip")
        with zipfile.ZipFile(zip_p, "w") as zf:
            for p in rendered:
                zf.write(p, os.path.basename(p))

    return (f"✅ Rendered {len(rendered)} clips via **{engine}**!", full_out, full_out, zip_p, *labels, *videos, *files)

with gr.Blocks(theme=gr.themes.Soft(primary_hue="violet"), title="ClipStudio AI") as demo:
    gr.Markdown("## ✂️ ClipStudio AI — Cloud YouTube Clipper")
    yt_url = gr.Textbox(label="YouTube or Livestream URL")
    api_key = gr.Textbox(label="Gemini API Key (Optional)", type="password")
    with gr.Row():
        num_clips = gr.Slider(1, MAX_CLIPS, value=3, step=1, label="Separate Clips")
        aspect = gr.Dropdown(["9:16 (Reels / Shorts)", "1:1 (Square)", "16:9 (Landscape)"], value="9:16 (Reels / Shorts)", label="Aspect Ratio")
        pan_x = gr.Slider(-50, 50, value=0, step=5, label="Pan X")
    with gr.Row():
        render_full = gr.Checkbox(value=True, label="Render Full Video with Audio")
        burn_subs = gr.Checkbox(value=True, label="Burn YouTube Subtitles (Full Video)")
    btn = gr.Button("🚀 Generate All Clips", variant="primary")
    status = gr.Markdown()

    with gr.Row():
        full_vid = gr.Video(label="Full Video Preview")
        full_dl = gr.File(label="⬇️ Download Full Video")
        zip_dl = gr.File(label="📦 Download All Clips (.ZIP)")

    c_lbls, c_vids, c_dls = [], [], []
    for i in range(MAX_CLIPS):
        l = gr.Markdown(visible=False)
        with gr.Row():
            v = gr.Video(label=f"Clip {i+1}", visible=False)
            f = gr.File(label=f"⬇️ Download Clip {i+1}", visible=False)
        c_lbls.append(l); c_vids.append(v); c_dls.append(f)

    btn.click(process_video, [yt_url, api_key, num_clips, aspect, pan_x, render_full, burn_subs], [status, full_vid, full_dl, zip_dl, *c_lbls, *c_vids, *c_dls])

demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))
      
