#!/bin/bash
# runroute: Google Drive 원본 폴더 → 로컬 git 작업본(~/runroute-git) → GitHub(HongSeongHun/runroute) 동기화
# 원본은 항상 이 스크립트가 있는 폴더. courses/, index.v*.html, .DS_Store 는 공개 저장소에서 제외.
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/runroute-git"
REPO="HongSeongHun/runroute"

if [ ! -d "$DST/.git" ]; then
  mkdir -p "$DST" && git -C "$DST" init -q -b main
fi
rsync -a --delete \
  --exclude '.git/' --exclude '.DS_Store' --exclude 'courses/' --exclude 'index.v*.html' --exclude '_test/' --exclude 'relay/test*' --exclude 'relay/check_rule.py' --exclude '__pycache__/' \
  "$SRC/" "$DST/"
printf '.DS_Store\ncourses/\nindex.v*.html\n_test/\nrelay/test*\nrelay/check_rule.py\n__pycache__/\n' > "$DST/.gitignore"

cd "$DST"
git add -A
if git diff --cached --quiet; then echo "변경 없음"; else
  git commit -q -m "sync: $(date '+%Y-%m-%d %H:%M')"; fi

if ! gh repo view "$REPO" >/dev/null 2>&1; then
  gh repo create "$REPO" --public --description "RunRoute Planner - 목표 거리 러닝 코스 생성 → Garmin 코스(TCX/GPX)" --source=. --remote=origin --push
  gh api -X POST "repos/$REPO/pages" -f "source[branch]=main" -f "source[path]=/" >/dev/null
else
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$REPO.git"
  git push -u origin main
fi
echo "repo : https://github.com/$REPO"
echo "pages: $(gh api repos/$REPO/pages --jq .html_url 2>/dev/null || echo '(설정 중)')"
