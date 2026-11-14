#!/usr/bin/env python3
"""Build a vertical cinematic reel (1080x1920) from an edit list.

Edit list format (one shot per line, '#' starts a comment):
    input/clip.mov  START  END  [in|out]  [CX]
START/END are seconds in the source clip (for a photo, END-START is how long
it stays on screen). in/out picks the slow zoom direction (default: alternate).
CX (0..1) is where to centre the 9:16 crop on a horizontal shot (default 0.5).
iPhone HDR (HLG) clips are tone-mapped to SDR so they match the other shots.

Usage:
    python3 make_reel.py edl.txt output/reel.mp4 --title COPENHAGEN --sub "DENMARK  ·  DAY 1"
"""
import argparse
import os
import shlex
import subprocess
import tempfile

FPS = 30
W, H = 1080, 1920
XF = 0.35  # cross-fade length between shots (s)
BOLD = "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf"
LIGHT = "/usr/share/fonts/opentype/inter/InterDisplay-Light.otf"

# The grade the user liked on the first clip.
GRADE = (
    "eq=contrast=1.10:saturation=1.18:gamma=0.96,"
    "colorbalance=rs=0.04:gs=0.01:bs=-0.05:rm=0.03:bm=-0.03,"
    "vignette=PI/5,noise=alls=5:allf=t"
)


def run(cmd):
    subprocess.run(cmd, check=True)


IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".webp")
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")


def probe_video(path, entry):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={entry}", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip().strip(",")


def has_audio(path):
    """Index of the first decodable audio stream (iPhone files also carry
    spatial-audio tracks ffmpeg can't decode), or None."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.split()
    for line in out:
        idx, *codec = line.split(",")
        if codec and codec[0] in ("aac", "mp3", "opus", "pcm_s16le", "alac", "ac3"):
            return idx
    return None


def render_shot(src, start, end, zoom, cx, out):
    dur = end - start
    frames = round(dur * FPS)
    z = (f"1+0.08*on/{frames}" if zoom == "in"
         else f"1.08-0.08*on/{frames}")
    is_img = src.lower().endswith(IMAGE_EXT)
    hdr = (not is_img) and probe_video(src, "color_transfer") in ("arib-std-b67", "smpte2084")
    vf = (
        (TONEMAP if hdr else "")
        + f"fps={FPS},scale={2*W}:{2*H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={2*W}:{2*H}:x='(iw-{2*W})*{cx}':y='(ih-{2*H})/2',"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
        f"{GRADE},format=yuv420p"
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if is_img:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", str(dur), "-i", src]
    else:
        cmd += ["-ss", str(start), "-t", str(dur), "-i", src]
    aidx = None if is_img else has_audio(src)
    if aidx is None:
        cmd += ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a"
    else:
        amap = f"0:{aidx}"
    cmd += ["-map", "0:v:0", "-map", amap, "-vf", vf,
            "-af", "aresample=48000,highpass=f=80",
            "-t", str(dur),
            "-c:v", "libx264", "-preset", "medium", "-crf", "16",
            "-c:a", "pcm_s16le", "-ac", "2", out]
    run(cmd)
    return dur


def text_filter(title, sub, until):
    a = (f"if(lt(t,0.4),0,if(lt(t,1.1),(t-0.4)/0.7,"
         f"if(lt(t,{until}-0.6),1,max(0,({until}-t)/0.6))))")
    esc = lambda s: s.replace(":", "\\:").replace("'", "’")
    f = (f"drawtext=fontfile={BOLD}:text='{esc(title)}':fontsize=120:fontcolor=white:"
         f"alpha='{a}':x=(w-text_w)/2:y=h*0.75:shadowcolor=black@0.55:shadowx=0:shadowy=4")
    if sub:
        f += (f",drawtext=fontfile={LIGHT}:text='{esc(sub)}':fontsize=46:fontcolor=white:"
              f"alpha='{a}':x=(w-text_w)/2:y=h*0.75+150:shadowcolor=black@0.55:shadowx=0:shadowy=3")
    return f


def main():
    p = argparse.ArgumentParser()
    p.add_argument("edl")
    p.add_argument("out")
    p.add_argument("--title", default="")
    p.add_argument("--sub", default="")
    p.add_argument("--music", help="optional music file mixed under the ambient sound")
    args = p.parse_args()

    shots = []
    with open(args.edl) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                shots.append(shlex.split(line))

    tmp = tempfile.mkdtemp(prefix="reel_")
    parts, durs = [], []
    for i, s in enumerate(shots):
        zoom = s[3] if len(s) > 3 else ("in" if i % 2 == 0 else "out")
        cx = float(s[4]) if len(s) > 4 else 0.5
        out = os.path.join(tmp, f"shot{i:02d}.mov")
        durs.append(render_shot(s[0], float(s[1]), float(s[2]), zoom, cx, out))
        parts.append(out)
        print(f"shot {i+1}/{len(shots)} done")

    # Chain cross-fades.
    inputs, fc = [], []
    for part in parts:
        inputs += ["-i", part]
    v, a, t = "[0:v]", "[0:a]", durs[0]
    for i in range(1, len(parts)):
        off = t - XF
        fc.append(f"{v}[{i}:v]xfade=transition=fade:duration={XF}:offset={off:.3f}[v{i}]")
        fc.append(f"{a}[{i}:a]acrossfade=d={XF}[a{i}]")
        v, a = f"[v{i}]", f"[a{i}]"
        t += durs[i] - XF
    total = t

    post_v = []
    if args.title:
        post_v.append(text_filter(args.title, args.sub, min(3.5, total - 0.5)))
    post_v.append(f"fade=t=in:st=0:d=0.4,fade=t=out:st={total-0.7:.3f}:d=0.7,format=yuv420p")
    fc.append(f"{v}{','.join(post_v)}[vout]")

    amix_in = a
    if args.music:
        inputs += ["-i", args.music]
        mi = len(parts)
        fc.append(f"[{mi}:a]atrim=0:{total:.3f},volume=0.35[mus]")
        fc.append(f"{a}[mus]amix=inputs=2:duration=first:normalize=0[mix]")
        amix_in = "[mix]"
    fc.append(f"{amix_in}loudnorm=I=-14:TP=-1.5,"
              f"afade=t=in:st=0:d=0.4,afade=t=out:st={total-0.7:.3f}:d=0.7[aout]")

    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
         "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-maxrate", "16M", "-bufsize", "32M", "-profile:v", "high",
         "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", args.out])
    print(f"done: {args.out} ({total:.1f}s)")


if __name__ == "__main__":
    main()
