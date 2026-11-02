#!/usr/bin/env bash
# Record a live HLS (.m3u8) URL copied from your own browser, for streams the
# monitor cannot detect (e.g. Amazon IVS streams on RPlay).
#   scripts/record_m3u8.sh                  # asks for the URL (paste it, press Enter)
#   scripts/record_m3u8.sh <url> [creator]  # creator defaults to $DEFAULT_CREATOR or "manual"
#
# Records into hidden .ts parts under archive/<creator>/, reconnects on drops,
# then joins the parts into archive/<creator>/<creator> <time>.mp4.
# Ctrl+C stops recording and still saves what was recorded.
#
# Env: ARCHIVE_DIR (default <repo>/archive), DEFAULT_CREATOR,
#      RECORD_HOOK  optional command run at the end as:
#                   saved <mp4> <parts> | merge_failed <stem> <dir> | nothing <stem>
# Needs ffmpeg and ffprobe on PATH. The URL is never written to the log.
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root" || exit 1

URL="$1"
CREATOR="${2:-${DEFAULT_CREATOR:-manual}}"
[ -n "$URL" ] || read -r -p "m3u8 URL: " URL
case "$URL" in http*://*) ;; *) echo "Not a URL." >&2; exit 1 ;; esac
case "$CREATOR" in .*|*/*|*\\*) echo "Invalid creator name." >&2; exit 1 ;; esac

out_dir="${ARCHIVE_DIR:-$root/archive}/$CREATOR"
mkdir -p "$out_dir" logs
stem="$CREATOR $(date +%Y-%m-%d_%H%M%S)"
log="logs/record_m3u8.log"
stop=0
trap 'stop=1' INT TERM

say() { echo "$(date '+%F %T') [$stem] $*" | tee -a "$log"; }
hook() { [ -n "$RECORD_HOOK" ] && $RECORD_HOOK "$@"; }

parts=()
quick_fails=0
for n in $(seq -w 1 50); do
  part="$out_dir/.${stem}_r${n}.ts"
  start=$(date +%s)
  say "part $n start"
  ffmpeg -hide_banner -loglevel warning -nostdin \
    -rw_timeout 30000000 -i "$URL" -c copy -f mpegts "$part" 2>>"$log"
  status=$?
  [ -s "$part" ] && parts+=("$part") || rm -f "$part"
  say "part $n exit $status"

  [ $stop -eq 1 ] && break
  [ $status -eq 0 ] && break            # stream ended normally
  if [ $(( $(date +%s) - start )) -lt 15 ]; then
    quick_fails=$((quick_fails + 1))
    # Repeated instant failures: stream is over or the URL expired.
    [ $quick_fails -ge 3 ] && { say "URL stopped working (ended or expired)."; break; }
  else
    quick_fails=0
  fi
  sleep 5
done

if [ ${#parts[@]} -eq 0 ]; then
  say "Nothing was recorded."
  hook nothing "$stem"
  exit 1
fi

# Hidden while merging so tools watching the folder only see finished files.
tmp="$out_dir/.${stem}.mp4"
final="$out_dir/${stem}.mp4"
concat="concat:$(IFS='|'; echo "${parts[*]}")"
if ffmpeg -hide_banner -loglevel error -nostdin -i "$concat" -map 0 -c copy \
     -bsf:a aac_adtstoasc -movflags +faststart "$tmp" 2>>"$log"; then
  mv "$tmp" "$final"
  rm -f "${parts[@]}"
  say "Saved: $final"
  hook saved "$final" "${#parts[@]}"
else
  rm -f "$tmp"
  say "Merge failed; raw parts kept in $out_dir (hidden .ts files)."
  hook merge_failed "$stem" "$out_dir"
  exit 1
fi
