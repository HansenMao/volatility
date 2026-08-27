#!/usr/bin/env bash
#
# build_windows_github.sh — build volkit.exe on GitHub's Windows runner and
# bring it back here.
#
#   ./build_windows_github.sh                 standalone exe; Analysis, Market
#                                             maker and Monitor hidden
#   ./build_windows_github.sh --explain       why did the last run fail?
#   ./build_windows_github.sh --folder        one folder instead of one file
#   ./build_windows_github.sh --hidden-tab mm --hidden-tab analysis
#   ./build_windows_github.sh --exclude-tab listed
#   ./build_windows_github.sh -m "message"    commit message for what it commits
#
# The runner builds what is *pushed*, so anything left in the working tree
# would silently not be in the exe.  Rather than refuse, this commits and
# pushes it first, prints exactly what it committed, and only then dispatches.
# --no-commit refuses instead; --allow-dirty builds the pushed commit and
# leaves local changes alone.
#
# PyInstaller cannot cross-compile: it bundles the host interpreter and
# host-compiled C extensions, so a Windows .exe can only be produced on
# Windows.  This script therefore does not build anything itself.  It drives
# the build-windows workflow on a hosted Windows runner and does the four
# tedious parts around it: check that what is about to be built is what is
# actually pushed, dispatch the run, wait for it, and unwrap the result.
#
# When a run fails it prints the failing step's log rather than leaving you
# with "process completed with exit code 1", which is the whole reason this
# exists.
#
# Needs: git, curl, python3, unzip, and one of
#   * the GitHub CLI, authenticated:   brew install gh && gh auth login
#   * a token in $GITHUB_TOKEN with the "actions:write" and "contents:read"
#     scopes (a fine-grained token needs Actions: read and write).

set -euo pipefail

WORKFLOW="build-windows.yml"
ARTIFACT="volkit-windows"
ONEFILE="true"
HIDDEN="analysis,mm,monitor"
HIDDEN_SET=""
EXCLUDE=""
BRANCH=""
OUT=""
WAIT_SECONDS=3600
POLL=15
EXPLAIN_ONLY=""
NO_WAIT=""
ALLOW_DIRTY=""
NO_COMMIT=""
ALLOW_LARGE=""
MESSAGE=""
# An untracked file this big is almost certainly build output, not source.
# 84 MB of dist/ went within one "git add -A" of being committed once.
MAX_ADD_BYTES=$((10 * 1024 * 1024))

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\n%s\n\n' "error: $*" >&2; exit 1; }

usage() {
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
}

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --onefile)      ONEFILE="true" ;;
    --folder)       ONEFILE="false" ;;
    --hidden-tab)   [ $# -ge 2 ] || die "--hidden-tab needs a screen name"
                    # The first --hidden-tab replaces the default rather than
                    # adding to it, so "--hidden-tab mm" hides mm and nothing else.
                    [ -n "$HIDDEN_SET" ] || { HIDDEN=""; HIDDEN_SET=1; }
                    HIDDEN="${HIDDEN:+$HIDDEN,}$2"; shift ;;
    --no-hidden)    HIDDEN=""; HIDDEN_SET=1 ;;
    --exclude-tab)  [ $# -ge 2 ] || die "--exclude-tab needs a screen name"
                    EXCLUDE="${EXCLUDE:+$EXCLUDE,}$2"; shift ;;
    --branch)       [ $# -ge 2 ] || die "--branch needs a name"; BRANCH="$2"; shift ;;
    --out)          [ $# -ge 2 ] || die "--out needs a directory"; OUT="$2"; shift ;;
    --explain)      EXPLAIN_ONLY="last"
                    if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then EXPLAIN_ONLY="$2"; shift; fi ;;
    --no-wait)      NO_WAIT=1 ;;
    --allow-dirty)  ALLOW_DIRTY=1 ;;
    --no-commit)    NO_COMMIT=1 ;;
    --allow-large)  ALLOW_LARGE=1 ;;
    -m|--message)   [ $# -ge 2 ] || die "-m needs a commit message"; MESSAGE="$2"; shift ;;
    -h|--help)      usage ;;
    *)              die "unknown option $1  (try --help)" ;;
  esac
  shift
done

for tool in git curl python3 unzip; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
cd "$ROOT"
[ -z "$BRANCH" ] && BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ -z "$OUT" ] && OUT="$ROOT/dist-windows"

REMOTE=$(git remote get-url origin 2>/dev/null) || die "this repository has no 'origin' remote"
SLUG=$(printf '%s' "$REMOTE" | sed -E 's#^git@github\.com:#https://github.com/#; s#^https://[^/]*/##; s#\.git$##')
case "$SLUG" in
  */*) : ;;
  *)   die "origin is not a GitHub repository: $REMOTE" ;;
esac

# ---------------------------------------------------------------------- auth
# The GitHub CLI is preferred because it already holds a credential.  Without
# it every call goes through curl and needs a token; either way the rest of
# the script talks to one api() function and does not care which.
GH=""
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  GH=1
fi
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -z "$GH" ] && [ -z "$TOKEN" ]; then
  die "no GitHub credential.

  Either install and log in to the GitHub CLI:
      brew install gh && gh auth login

  or export a token with the Actions read/write scope:
      export GITHUB_TOKEN=ghp_...

  Both are one-time; this script does not store anything."
fi

api() {           # api GET|POST path [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$GH" ]; then
    if [ -n "$body" ]; then
      printf '%s' "$body" | gh api --method "$method" "$path" --input -
    else
      gh api --method "$method" "$path"
    fi
  else
    local args=(-sS -X "$method"
      -H "Accept: application/vnd.github+json"
      -H "Authorization: Bearer $TOKEN"
      -H "X-GitHub-Api-Version: 2022-11-28")
    [ -n "$body" ] && args+=(-d "$body")
    # curl reports HTTP 401 and 404 as success unless asked not to, and an
    # empty body would then be read as "no runs found" -- a silent zero by
    # another route.  The status code is checked and the API's own message is
    # what gets printed.
    local raw code payload
    raw=$(curl -sS -w $'\n%{http_code}' "${args[@]}" "https://api.github.com/$path") \
      || { warn "curl could not reach api.github.com"; return 1; }
    code=${raw##*$'\n'}
    payload=${raw%$'\n'*}
    case "$code" in
      2*) printf '%s' "$payload" ;;
      *)  warn "GitHub API returned $code for $path"
          printf '%s' "$payload" | py 'import json,sys
try: print("    " + json.load(sys.stdin).get("message","")) 
except Exception: pass' >&2
          return 1 ;;
    esac
  fi
}

api_download() {  # api_download url outfile
  if [ -n "$GH" ]; then
    gh api "$1" > "$2"
  else
    curl -sSL -H "Authorization: Bearer $TOKEN" \
         -H "Accept: application/vnd.github+json" "https://api.github.com/$1" -o "$2"
  fi
}

py() { python3 -c "$@"; }

# ------------------------------------------------------------------ explain
# The failing step's own output.  A run page says "process completed with exit
# code 1" and buries the reason several clicks away; this puts it on the
# terminal, which is where the person who dispatched the build is standing.
print_failure() {
  local run_id="$1"
  step "Why it failed"
  api GET "repos/$SLUG/actions/runs/$run_id/jobs" | py '
import json, sys
jobs = json.load(sys.stdin).get("jobs", [])
hit = False
for j in jobs:
    for st in j.get("steps", []):
        if st.get("conclusion") in ("failure", "cancelled", "timed_out"):
            hit = True
            print("  %s -> step %s: %s  [%s]" % (
                j.get("name", "?"), st.get("number", "?"),
                st.get("name", "?"), st.get("conclusion")))
if not hit:
    print("  no failed step reported; the job may have been cancelled or the runner died")
' || warn "could not list the failed steps"
  if [ -n "$GH" ]; then
    say ""
    gh run view "$run_id" --repo "$SLUG" --log-failed 2>/dev/null | tail -60 \
      || warn "could not read the step log"
    return
  fi
  local tmp; tmp=$(mktemp -d)
  if api_download "repos/$SLUG/actions/runs/$run_id/logs" "$tmp/logs.zip" 2>/dev/null \
     && unzip -qo "$tmp/logs.zip" -d "$tmp/logs" 2>/dev/null; then
    say ""
    # The largest step log is the build; its tail carries build_exe.py's own
    # "BUILD FAILED" line and the reason underneath it.
    local biggest
    biggest=$(find "$tmp/logs" -type f -name '*.txt' -exec ls -S {} + 2>/dev/null | head -1)
    [ -n "$biggest" ] && tail -60 "$biggest"
  else
    warn "could not download the logs; open the run page instead"
  fi
  rm -rf "$tmp"
}

latest_run_id() {
  api GET "repos/$SLUG/actions/workflows/$WORKFLOW/runs?per_page=1" \
    | py 'import json,sys; r=json.load(sys.stdin).get("workflow_runs",[]); print(r[0]["id"] if r else "")'
}

if [ -n "$EXPLAIN_ONLY" ]; then
  if [ "$EXPLAIN_ONLY" = "last" ]; then
    RUN_ID=$(latest_run_id)
    [ -n "$RUN_ID" ] || die "no runs of $WORKFLOW found in $SLUG"
  else
    RUN_ID="$EXPLAIN_ONLY"
  fi
  api GET "repos/$SLUG/actions/runs/$RUN_ID" | py '
import json, sys
r = json.load(sys.stdin)
print("  run %s  %s/%s  %s" % (r["id"], r["status"], r.get("conclusion"), r["created_at"]))
print("  " + r["html_url"])' || warn "could not read the run"
  print_failure "$RUN_ID"
  exit 0
fi

# --------------------------------------------------------------- preflight
# The runner builds what is on GitHub, not what is in this directory.  Saying
# so before a fifteen-minute wait is cheaper than discovering it afterwards.
step "Preflight"
say "  repository  $SLUG"
say "  branch      $BRANCH"
say "  layout      $([ "$ONEFILE" = true ] && echo 'one file (standalone volkit.exe)' || echo 'one folder')"
say "  hidden      ${HIDDEN:-(none)}"
[ -n "$EXCLUDE" ] && say "  excluded    $EXCLUDE"

git symbolic-ref -q HEAD >/dev/null \
  || die "HEAD is detached, so there is no branch to push. Check one out first."

# Anything still in the working tree would silently not be in the exe, because
# the runner builds the pushed commit.  Commit it rather than refusing -- but
# never sweep in build output, which is what an unguarded "git add -A" does.
DIRTY=$(git status --porcelain --untracked-files=all)
if [ -n "$DIRTY" ] && [ -n "$ALLOW_DIRTY" ]; then
  warn "local changes left alone (--allow-dirty); building the pushed commit instead"
elif [ -n "$DIRTY" ] && [ -n "$NO_COMMIT" ]; then
  die "there are uncommitted changes and --no-commit was given, so the exe would
  not contain them. Commit them, or drop --no-commit and this will."
elif [ -n "$DIRTY" ]; then
  say ""
  say "  uncommitted changes — committing them, or the exe will not contain them:"
  git -c core.quotepath=false status --short --untracked-files=all | sed 's/^/    /'

  if [ -z "$ALLOW_LARGE" ]; then
    BIG=""
    while IFS= read -r -d '' f; do
      [ -f "$f" ] || continue
      SZ=$(wc -c < "$f" | tr -d ' ')
      if [ "$SZ" -gt "$MAX_ADD_BYTES" ]; then
        BIG="$BIG
    $f  ($((SZ / 1048576)) MB)"
      fi
    done < <(git ls-files --others --exclude-standard -z)
    [ -n "$BIG" ] && die "these untracked files are too big to be source:
$BIG

  A build directory does not belong in the repository. Add it to .gitignore,
  or pass --allow-large if you really mean to commit it. Nothing was committed."
  fi

  [ -n "$MESSAGE" ] || MESSAGE="Windows build$([ -n "$HIDDEN" ] && echo " (hidden: $HIDDEN)")"
  git add -A
  git commit -q -m "$MESSAGE" || die "the commit failed; nothing was pushed"
  say "  committed   $(git rev-parse --short HEAD)  \"$MESSAGE\""
fi

git fetch --quiet origin "$BRANCH" 2>/dev/null || warn "could not fetch origin/$BRANCH"
LOCAL=$(git rev-parse HEAD)
UPSTREAM=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")
if [ -z "$UPSTREAM" ]; then
  say "  pushing     new branch $BRANCH"
  git push -q -u origin "$BRANCH" || die "the push failed; nothing was dispatched"
elif [ "$LOCAL" != "$UPSTREAM" ]; then
  if git merge-base --is-ancestor "$UPSTREAM" HEAD 2>/dev/null; then
    say "  pushing     $(git rev-list --count "$UPSTREAM"..HEAD) commit(s) to origin/$BRANCH"
    git push -q origin "$BRANCH" || die "the push failed; nothing was dispatched"
  else
    # Never resolve this automatically: a merge or a rebase is a decision
    # about someone else's work, not a build step.
    die "origin/$BRANCH has commits you do not have, so pushing would not
  fast-forward. Pull or rebase first, then run this again.
    local  $LOCAL
    remote $UPSTREAM"
  fi
fi

UPSTREAM=$(git rev-parse "origin/$BRANCH")
LOCAL=$(git rev-parse HEAD)
[ "$LOCAL" = "$UPSTREAM" ] || die "origin/$BRANCH is still not at HEAD after pushing"
say "  commit      ${LOCAL:0:12} (pushed)"

# The dispatch form is read from the workflow file on the *default* branch, so
# an input this script sends but that file does not declare is dropped in
# silence -- and the build would quietly come back with every tab showing.
for input in onefile hidden_tabs exclude_tabs; do
  git show "$UPSTREAM:.github/workflows/$WORKFLOW" 2>/dev/null | grep -q "^      $input:" \
    || die "the workflow on origin/$BRANCH has no '$input' input, so this build would
  silently ignore it. Push the current .github/workflows/$WORKFLOW first."
done
say "  workflow    $WORKFLOW declares onefile, hidden_tabs, exclude_tabs"

# ---------------------------------------------------------------- dispatch
BODY=$(py "
import json
print(json.dumps({'ref': '$BRANCH', 'inputs': {
    'onefile': '$ONEFILE',
    'hidden_tabs': '$HIDDEN',
    'exclude_tabs': '$EXCLUDE',
}}))")

step "Dispatching"
DISPATCHED_AT=$(date -u +%s)
api POST "repos/$SLUG/actions/workflows/$WORKFLOW/dispatches" "$BODY" >/dev/null \
  || die "the dispatch was refused. The token needs Actions read/write, and the
  workflow file must be on the repository's default branch."
say "  requested; finding the run"

RUN_ID=""
for _ in $(seq 1 20); do
  sleep 3
  RUN_ID=$(api GET "repos/$SLUG/actions/workflows/$WORKFLOW/runs?branch=$BRANCH&event=workflow_dispatch&per_page=10" \
    | py "
import json, sys, datetime
since = $DISPATCHED_AT - 120
best = None
for r in json.load(sys.stdin).get('workflow_runs', []):
    t = datetime.datetime.strptime(r['created_at'], '%Y-%m-%dT%H:%M:%SZ')
    t = t.replace(tzinfo=datetime.timezone.utc).timestamp()
    if t < since:
        continue
    if best is None or t > best[0]:
        best = (t, r['id'])
print(best[1] if best else '')")
  [ -n "$RUN_ID" ] && break
done
[ -n "$RUN_ID" ] || die "dispatched, but no run appeared. Check the Actions tab."

RUN_URL="https://github.com/$SLUG/actions/runs/$RUN_ID"
say "  run $RUN_ID"
say "  $RUN_URL"

if [ -n "$NO_WAIT" ]; then
  say ""
  say "Not waiting (--no-wait). When it finishes:"
  say "  $0 --explain $RUN_ID        # if it failed"
  exit 0
fi

# -------------------------------------------------------------------- wait
step "Waiting"
say "  the runner installs dependencies, runs 344 tests, builds, stages the data"
say "  files and smoke-tests the exe. Around fifteen minutes."
START=$(date -u +%s)
STATUS=""; CONCLUSION=""
while :; do
  read -r STATUS CONCLUSION <<EOF
$(api GET "repos/$SLUG/actions/runs/$RUN_ID" | py 'import json,sys; r=json.load(sys.stdin); print(r["status"], r.get("conclusion") or "-")' || true)
EOF
  if [ -z "$STATUS" ]; then
    warn "could not read the run status; retrying"
    STATUS="unknown"
  fi
  ELAPSED=$(( $(date -u +%s) - START ))
  printf '\r  %-12s %3dm %02ds ' "$STATUS" $((ELAPSED / 60)) $((ELAPSED % 60))
  [ "$STATUS" = "completed" ] && break
  if [ "$ELAPSED" -gt "$WAIT_SECONDS" ]; then
    printf '\n'
    die "still running after $((WAIT_SECONDS / 60)) minutes; watch it at $RUN_URL"
  fi
  sleep "$POLL"
done
printf '\n'

if [ "$CONCLUSION" != "success" ]; then
  say "  conclusion: $CONCLUSION"
  print_failure "$RUN_ID"
  say ""
  die "the build did not succeed. $RUN_URL"
fi
say "  success"

# ---------------------------------------------------------------- download
step "Downloading"
ART_ID=$(api GET "repos/$SLUG/actions/runs/$RUN_ID/artifacts" | py "
import json, sys
for a in json.load(sys.stdin).get('artifacts', []):
    if a['name'] == '$ARTIFACT' and not a.get('expired'):
        print(a['id']); break")
[ -n "$ART_ID" ] || die "the run succeeded but produced no '$ARTIFACT' artifact"

rm -rf "$OUT"; mkdir -p "$OUT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

api_download "repos/$SLUG/actions/artifacts/$ART_ID/zip" "$TMP/outer.zip"
unzip -qo "$TMP/outer.zip" -d "$TMP/outer" || die "the downloaded artifact is not a zip"

# GitHub wraps every artifact in a zip of its own, and this artifact *is* a
# zip, so the exe is two layers down.  Unwrap both so what lands in $OUT is
# the thing you hand over.
INNER=$(find "$TMP/outer" -name '*.zip' | head -1)
if [ -n "$INNER" ]; then
  unzip -qo "$INNER" -d "$OUT" || die "the inner archive would not open"
else
  cp -R "$TMP/outer/." "$OUT/"
fi

EXE=$(find "$OUT" -name 'volkit.exe' | head -1)
[ -n "$EXE" ] || die "no volkit.exe in the artifact; look in $OUT"

# ------------------------------------------------------------------ report
step "Done"
say "  $EXE"
say "  $(du -sh "$OUT" | cut -f1) in $OUT"
say ""
say "  Contents:"
find "$OUT" -maxdepth 2 -type f | sed "s#^$OUT/#    #" | sort | head -20
say ""
if [ -n "$HIDDEN" ]; then
  FIRST="${HIDDEN%%,*}"
  say "  Hidden in this build: $HIDDEN"
  say "  On the Windows machine, double-click volkit.exe and the tab is not there."
  say "  To turn it on for one run:   volkit.exe --enable-tab $FIRST"
  say "  To turn it on permanently:   add 'enable-tab = $FIRST' to volkit.cfg"
  say ""
fi
say "  Hand over the whole folder: the workbook, feed, bands and volkit.cfg beside"
say "  the exe are meant to be edited without rebuilding."
