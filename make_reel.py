#!/usr/bin/env python3
"""Cinematic vertical reel builder: 1080x1920, 24 fps, cuts on the music beat.

Edit list (one shot per line, '#' starts a comment):
    input/clip.mov  in=0.3  beats=8  zoom=in  cx=0.5  speed=0.4  trans=fade:16  label=NYHAVN
  in      start time in the source, seconds (ignored for photos)
  beats   time on screen, cut point to cut point, in beats of the music
  zoom    in | out | punch (quick push-back, for a beat drop) | none
  cx      0..1, horizontal centre of the 9:16 crop on landscape footage
  speed   1 = real time, 0.4 = slow motion from 60 fps footage (exact 24 fps)
  trans   transition INTO this shot: cut | fade:<frames>
  label   small location tag shown on this shot
  gamma   per-shot exposure tweak (default 1)

Look = the colour of the first Cafe du Nord edit. iPhone HLG footage is graded
as-is (that is what produced the look); SDR footage (DJI, photos) is first mapped
to the same HLG-style tonality so every shot matches. Then the grade, a soft
highlight glow, vignette and fine luma grain.

Usage:
    python3 make_reel.py edl.txt out.mp4 --music music.wav --bpm 96 \\
        --title COPENHAGEN --sub "DENMARK  ·  DAY 1"
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shlex
import subprocess
import tempfile

FPS = 24
W, H = 1080, 1920
BOLD = "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf"
LIGHT = "/usr/share/fonts/opentype/inter/InterDisplay-Light.otf"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]

# The grade the user liked on the first clip.
GRADE = ("eq=contrast=1.10:saturation=1.18:gamma=0.96,"
         "colorbalance=rs=0.04:gs=0.01:bs=-0.05:rm=0.03:bm=-0.03")
# Soft warm glow on the brightest parts (halation), vignette, fine grain.
FINISH = ("format=gbrp,split[base][hl];"
          "[hl]curves=all='0/0 0.72/0 1/1',gblur=sigma=26,"
          "colorchannelmixer=rr=1.0:gg=0.84:bb=0.66[glow];"
          "[base][glow]blend=all_mode=screen:all_opacity=0.28,"
          "vignette=PI/5,format=yuv420p,noise=c0s=7:c0f=t")
# SDR -> HLG-style tonality, so DJI clips and photos match the iPhone clips.
TO_HLG = "t=arib-std-b67:p=bt2020:m=2020_ncl:r=tv:npl=203,format=yuv420p10le,"
# Read every frame as plain BT.709, exactly how phones played the first edit.
# (Without this, the RGB step for the glow decodes iPhone frames as BT.2020 and
# the colours drift away from the look the user liked.)
AS_709 = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv,"


def run(cmd):
    subprocess.run(cmd, check=True)


def probe(path, args):
    out = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def source_info(path):
    if path.lower().endswith(IMAGE_EXT):
        return {"image": True, "hdr": False, "audio": None, "dur": 1e9}
    j = probe(path, ["-show_entries", "stream=index,codec_type,codec_name,color_transfer:format=duration"])
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    audio = next((s["index"] for s in j["streams"] if s["codec_type"] == "audio"
                  and s.get("codec_name") in ("aac", "mp3", "opus", "alac", "pcm_s16le")), None)
    return {"image": False, "hdr": v.get("color_transfer") in ("arib-std-b67", "smpte2084"),
            "audio": audio, "dur": float(j["format"]["duration"])}


def parse_edl(path):
    shots = []
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = shlex.split(line)
            opts = dict(p.split("=", 1) for p in parts[1:])
            trans = opts.get("trans", "cut")
            shots.append({
                "src": parts[0],
                "in": float(opts.get("in", 0)),
                "beats": float(opts["beats"]),
                "zoom": opts.get("zoom", "in"),
                "cx": float(opts.get("cx", 0.5)),
                "speed": float(opts.get("speed", 1)),
                "trans": 1 if trans == "cut" else int(trans.split(":")[1]),
                "label": opts.get("label", ""),
                "gamma": float(opts.get("gamma", 1)),
            })
    shots[0]["trans"] = 0
    return shots


def timeline(shots, bpm):
    """Frame-exact layout: every cut (centre of a transition) sits on a beat."""
    fpb = FPS * 60 / bpm
    assert abs(fpb - round(fpb)) < 1e-9, "pick a BPM that gives whole frames per beat"
    fpb = round(fpb)
    cuts, c = [], 0
    for s in shots:
        cuts.append(c)
        c += round(s["beats"] * fpb)
    total = c
    for i, s in enumerate(shots):
        # A hard cut (1-frame transition) completes ON the beat frame;
        # a fade is centred on the beat.
        s["S"] = cuts[i] - (s["trans"] + 1) // 2
        if i + 1 < len(shots):
            nxt = shots[i + 1]["trans"]
            s["E"] = cuts[i + 1] - (nxt + 1) // 2 + nxt
        else:
            s["E"] = total
        s["D"] = s["E"] - s["S"]
    return total


def zoom_expr(kind, n):
    if kind == "in":
        return f"1+0.08*on/{n}"
    if kind == "out":
        return f"1.08-0.08*on/{n}"
    if kind == "punch":
        p = 7
        return f"if(lt(on,{p}),1.14-0.14*(1-pow(1-on/{p},3)),1+0.04*(on-{p})/{n - p})"
    return "1.0"


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".reel_cache")


def cache_path(s):
    st = os.stat(s["src"])
    key = json.dumps([s["src"], st.st_size, int(st.st_mtime), s["in"], s["zoom"], s["cx"],
                      s["speed"], s["gamma"], s["D"], FPS, GRADE, FINISH, TO_HLG, AS_709, "v2"])
    return os.path.join(CACHE, hashlib.sha1(key.encode()).hexdigest()[:16] + ".mov")


def render_shot(s, info, out):
    cached = cache_path(s)
    if os.path.exists(cached):
        return cached
    os.makedirs(CACHE, exist_ok=True)
    out = cached + ".part.mov"
    _render(s, info, out)
    os.replace(out, cached)
    return cached


def _render(s, info, out):
    n = s["D"]
    dur = n / FPS
    src_dur = dur * s["speed"]
    if not info["image"] and s["in"] + src_dur > info["dur"] + 0.02:
        print(f"  ! {s['src']}: needs {s['in'] + src_dur:.2f}s, clip has {info['dur']:.2f}s "
              "(last frame will be held)")
    gamma = s["gamma"]
    if info["image"]:
        pre = f"zscale=rin=pc:min=470bg:tin=iec61966-2-1:pin=bt709:{TO_HLG}{AS_709}"
        gamma *= 1.10
    elif not info["hdr"]:
        pre = f"zscale=rin=tv:min=bt709:tin=bt709:pin=bt709:{TO_HLG}{AS_709}"
        gamma *= 1.10
    else:
        pre = AS_709
    if abs(gamma - 1) > 1e-6:
        pre += f"eq=gamma={gamma:.3f},"
    speed = "" if info["image"] else f"setpts=(PTS-STARTPTS)/{s['speed']},"
    vf = (
        f"{pre}{speed}fps={FPS},"
        f"scale={2 * W}:{2 * H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={2 * W}:{2 * H}:x='(iw-{2 * W})*{s['cx']}':y='(ih-{2 * H})/2',"
        f"zoompan=z='{zoom_expr(s['zoom'], n)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={W}x{H}:fps={FPS},"
        f"tpad=stop_mode=clone:stop_duration=2,{GRADE},{FINISH}"
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if info["image"]:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur + 0.5:.3f}", "-i", s["src"]]
    else:
        cmd += ["-ss", f"{s['in']:.3f}", "-t", f"{src_dur + 0.3:.3f}", "-i", s["src"]]
    use_audio = info["audio"] is not None and s["speed"] == 1
    if use_audio:
        amap = f"0:{info['audio']}"
    else:
        cmd += ["-f", "lavfi", "-t", f"{dur + 0.5:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a"
    cmd += ["-map", "0:v:0", "-map", amap, "-vf", vf,
            "-af", "aresample=48000,highpass=f=80,apad",
            "-frames:v", str(n), "-t", f"{dur:.4f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p", *TAGS,
            "-c:a", "pcm_s16le", "-ac", "2", "-ar", "48000", out]
    run(cmd)
    return out


def esc(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def rising_text(text, font, size, y, t0, t1, rise=36):
    """Text that fades in while rising into place, holds, then fades out."""
    p = f"min(1,max(0,(t-{t0:.3f})/0.9))"
    alpha = (f"if(lt(t,{t0:.3f}),0,if(lt(t,{t0 + 0.7:.3f}),(t-{t0:.3f})/0.7,"
             f"if(lt(t,{t1 - 0.6:.3f}),1,max(0,({t1:.3f}-t)/0.6))))")
    return (f"drawtext=fontfile={font}:text='{esc(text)}':fontsize={size}:fontcolor=white:"
            f"alpha='{alpha}':x=(w-text_w)/2:y='{y}+{rise}*pow(1-{p},3)':"
            f"shadowcolor=black@0.5:shadowx=0:shadowy=3:enable='between(t,{t0:.3f},{t1:.3f})'")


def spaced(text):
    return " ".join(text.upper())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edl")
    ap.add_argument("out")
    ap.add_argument("--music", required=True)
    ap.add_argument("--bpm", type=float, default=96)
    ap.add_argument("--title", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--ambience", type=float, default=0.30,
                    help="level of the clips' own sound under the music")
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args()

    shots = parse_edl(a.edl)
    total = timeline(shots, a.bpm)
    tsec = total / FPS
    print(f"{len(shots)} shots, {tsec:.2f}s")
    tmp = tempfile.mkdtemp(prefix="reel_")

    infos = [source_info(s["src"]) for s in shots]
    outs = [None] * len(shots)
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(render_shot, s, inf, None): i for i, (s, inf) in enumerate(zip(shots, infos))}
        for f in cf.as_completed(futs):
            i = futs[f]
            outs[i] = f.result()
            print(f"  shot {i + 1:2d} {os.path.basename(shots[i]['src']):24s} {shots[i]['D'] / FPS:5.2f}s")

    # ---- picture: chain the transitions, add text, fade in/out
    inputs, fc = [], []
    for o in outs:
        inputs += ["-i", o]
    v, au = "[0:v]", "[0:a]"
    for i in range(1, len(shots)):
        d = shots[i]["trans"] / FPS
        off = shots[i]["S"] / FPS
        fc.append(f"{v}[{i}:v]xfade=transition=fade:duration={d:.5f}:offset={off:.5f}[v{i}]")
        fc.append(f"{au}[{i}:a]acrossfade=d={d:.5f}[a{i}]")
        v, au = f"[v{i}]", f"[a{i}]"

    text = []
    if a.title:
        t1 = shots[0]["E"] / FPS - 1.0
        text.append(rising_text(a.title, BOLD, 120, "h*0.75", 0.5, t1))
        if a.sub:
            text.append(rising_text(a.sub, LIGHT, 46, "h*0.75+150", 0.75, t1))
    for s in shots:
        if s["label"]:
            t0 = max(s["S"], 0) / FPS + 0.4
            t1 = s["E"] / FPS - 0.35
            text.append(rising_text(spaced(s["label"]), LIGHT, 40, "h*0.86", t0, t1, rise=20))
    post = text + [f"fade=t=in:st=0:d=0.5,fade=t=out:st={tsec - 1.0:.3f}:d=1.0,format=yuv420p"]
    fc.append(f"{v}{','.join(post)}[vout]")
    fc.append(f"{au}atrim=0:{tsec:.4f}[aout]")
    video = os.path.join(tmp, "video.mp4")
    amb = os.path.join(tmp, "ambience.wav")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
         "-filter_complex", ";".join(fc),
         "-map", "[vout]", "-c:v", "libx264", "-preset", "slow", "-crf", "19",
         "-maxrate", "16M", "-bufsize", "32M", "-profile:v", "high", "-pix_fmt", "yuv420p",
         "-r", str(FPS), *TAGS, video,
         "-map", "[aout]", "-c:a", "pcm_s16le", amb])

    # ---- sound: music + a quiet bed of the real ambience, loudness for Instagram
    vd = subprocess.run(["ffmpeg", "-hide_banner", "-i", amb, "-af", "volumedetect", "-f", "null", "-"],
                        capture_output=True, text=True).stderr
    amb_peak = float(vd.split("max_volume:")[1].split("dB")[0]) if "max_volume:" in vd else -91
    if amb_peak > -60:
        mixf = (f"[0:a]atrim=0:{tsec:.4f},afade=t=out:st={tsec - 1.6:.3f}:d=1.6[m];"
                f"[1:a]loudnorm=I=-30:TP=-6,volume={a.ambience / 0.3:.3f}[amb];"
                f"[m][amb]amix=inputs=2:duration=first:normalize=0")
    else:  # every shot is slow motion or a photo: music only
        mixf = (f"[0:a]atrim=0:{tsec:.4f},afade=t=out:st={tsec - 1.6:.3f}:d=1.6,"
                f"anull[m];[1:a]anullsink;[m]anull")
    meas = subprocess.run(["ffmpeg", "-hide_banner", "-i", a.music, "-i", amb, "-filter_complex",
                           mixf + ",loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                          capture_output=True, text=True).stderr
    m = json.loads(meas[meas.rindex("{"):meas.rindex("}") + 1])
    ln = (f"loudnorm=I=-14:TP=-1.5:LRA=11:linear=true:measured_I={m['input_i']}:"
          f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
          f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}")
    audio = os.path.join(tmp, "mix.m4a")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", a.music, "-i", amb,
         "-filter_complex", mixf + f",{ln},aresample=48000", "-c:a", "aac", "-b:a", "256k", audio])

    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video, "-i", audio,
         "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest", "-movflags", "+faststart", a.out])
    print(f"done: {a.out} ({tsec:.2f}s)")


if __name__ == "__main__":
    main()
