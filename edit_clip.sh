#!/usr/bin/env bash
# Edit one vlog clip: trim, slow zoom, warm cinematic colour, vignette,
# title text, fade in/out and louder ambient sound.
#
# Usage: ./edit_clip.sh INPUT OUTPUT [START] [END] [TITLE] [SUBTITLE]
set -euo pipefail

IN=$1
OUT=$2
START=${3:-0.3}
END=${4:-5.3}
TITLE=${5:-COPENHAGEN}
SUB=${6:-DENMARK  ·  DAY 1}

DUR=$(echo "$END - $START" | bc -l)
FADE_OUT=$(echo "$DUR - 0.6" | bc -l)
FPS=30
FRAMES=$(printf "%.0f" "$(echo "$DUR * $FPS" | bc -l)")
BOLD=/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf
LIGHT=/usr/share/fonts/opentype/inter/InterDisplay-Light.otf

# Text fades in at 0.5s, stays, fades out 0.8s before the end.
ALPHA="if(lt(t,0.5),0,if(lt(t,1.2),(t-0.5)/0.7,if(lt(t,$DUR-0.8),1,max(0,($DUR-t)/0.6))))"

ffmpeg -hide_banner -loglevel error -y -ss "$START" -t "$DUR" -i "$IN" \
  -filter_complex "
    [0:v]fps=$FPS,scale=2160:3840:flags=lanczos,
      zoompan=z='1+0.10*on/$FRAMES':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=$FPS,
      eq=contrast=1.10:saturation=1.18:gamma=0.96,
      colorbalance=rs=0.04:gs=0.01:bs=-0.05:rm=0.03:bm=-0.03,
      vignette=PI/5,
      drawtext=fontfile=$BOLD:text='$TITLE':fontsize=120:fontcolor=white:alpha='$ALPHA':
        x=(w-text_w)/2:y=h*0.75:shadowcolor=black@0.55:shadowx=0:shadowy=4,
      drawtext=fontfile=$LIGHT:text='$SUB':fontsize=46:fontcolor=white:alpha='$ALPHA':
        x=(w-text_w)/2:y=h*0.75+150:shadowcolor=black@0.55:shadowx=0:shadowy=3,
      fade=t=in:st=0:d=0.5,fade=t=out:st=$FADE_OUT:d=0.6,format=yuv420p[v];
    [0:a]highpass=f=80,loudnorm=I=-20:TP=-2,
      afade=t=in:st=0:d=0.5,afade=t=out:st=$FADE_OUT:d=0.6[a]" \
  -map "[v]" -map "[a]" \
  -c:v libx264 -preset slow -crf 18 -c:a aac -b:a 192k -movflags +faststart "$OUT"
