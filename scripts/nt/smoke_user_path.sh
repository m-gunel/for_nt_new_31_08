#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  smoke_user_path.sh — быстрая проверка (5-10 мин): отработает ли фаза 3?
#
#  Повторяет пользовательский путь в сжатом виде БЕЗ монитора datahound:
#    полный сброс -> индексация корпуса -> поиск параллельно с индексацией
#    -> доиндексация новых файлов -> контроль живости сервиса
#  и выдаёт вердикт: РАБОТАЕТ / НЕ РАБОТАЕТ.
#
#  Отдельный скрипт: боевые нагрузчики (test_sber_indexer.sh,
#  sber_indexer_loadtest.py) не трогает и не зависит от них.
#
#  ИСПОЛЬЗОВАНИЕ (от пользователя, БЕЗ sudo):
#    ./smoke_user_path.sh
#    FILES=200 MINUTES=5 ./smoke_user_path.sh
#    NO_RESET=1 ./smoke_user_path.sh      # не сбрасывать индекс
# ═══════════════════════════════════════════════════════════════════════════════
set -u
umask 000

URL="${URL:-http://127.0.0.1:8000}"
DIR="${DIR:-$HOME/index_test_files}"
INDEXER_DIR="${INDEXER_DIR:-$PWD}"
FILES="${FILES:-150}"            # размер корпуса (мало — чтобы уложиться в минуты)
MINUTES="${MINUTES:-6}"          # общий бюджет времени
SEARCH_EVERY="${SEARCH_EVERY:-3}"  # поиск раз в N секунд во время индексации
REINDEX_FILES="${REINDEX_FILES:-10}"  # сколько файлов добавить в доиндексацию
NO_RESET="${NO_RESET:-0}"
# ВАЖНО: каталог результатов НЕ кладём рядом с индексируемой папкой — при
# перезапуске main.py права на него перехватываются и лог становится недоступен.
_UH="$(getent passwd "${SUDO_USER:-$(id -un)}" 2>/dev/null | cut -d: -f6)"; [ -z "$_UH" ] && _UH="$HOME"
OUT="${OUT:-$_UH/smoke_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$OUT" 2>/dev/null
chmod 777 "$OUT" 2>/dev/null || true
LOG="$OUT/smoke.log"
: > "$LOG" 2>/dev/null || true
chmod 666 "$LOG" 2>/dev/null || true
# Если писать в лог нельзя (каталог создан другим пользователем/root) — не спамим
# ошибками tee на каждой строке, а честно предупреждаем один раз.
if ! ( echo probe >> "$LOG" ) 2>/dev/null; then
    echo "ВНИМАНИЕ: не могу писать в $LOG — вывод только на экран."
    echo "  (каталог, вероятно, принадлежит другому пользователю; удали старые smoke_* )"
    LOG="/dev/null"
fi

if [ -t 1 ]; then
  R=$'\033[0;31m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; C=$'\033[0;36m'; B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'
else R=''; G=''; Y=''; C=''; B=''; D=''; N=''; fi
ts(){ date '+%H:%M:%S'; }
log(){
  local m="[$(ts)] $*"
  echo -e "$m"
  # Перенаправление >> открывает файл на уровне shell, поэтому 2>/dev/null на
  # printf не подавляет "Отказано в доступе". Заворачиваем в подоболочку целиком.
  if [ "$LOG" != "/dev/null" ]; then
    ( printf '%s\n' "$m" >> "$LOG" ) 2>/dev/null || true
  fi
  return 0
}
ok(){   log "  ${G}OK${N} $*"; }
bad(){  log "  ${R}!!${N} $*"; }
inf(){  log "  ${C}->${N} $*"; }
hdr(){  echo ""; log "${B}$*${N}"; }

# ── результаты для итогового вердикта ─────────────────────────────────────────
V_CONFIG="?"; V_INDEXED=0; V_SEARCH_OK=0; V_SEARCH_ERR=0
V_REINDEX="?"; V_ALIVE="?"; V_CRASH_AT=""

alive(){ [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/api/v1/health" 2>/dev/null)" = "200" ]; }
status_raw(){ curl -s --max-time 5 "$URL/api/v1/status" 2>/dev/null; }
jnum(){ printf '%s' "$1" | grep -oE "\"$2\"[ ]*:[ ]*[0-9]+" | grep -oE '[0-9]+' | head -1; }
jbool(){ printf '%s' "$1" | grep -oE "\"$2\"[ ]*:[ ]*(true|false)" | grep -oE '(true|false)' | head -1; }
cfg_json(){ printf '%s' "{\"folders\":[\"$DIR\"],\"email\":{\"enabled\":false,\"path_to_email_folder\":\"\",\"index_last_n_days\":90},\"sberchat\":{\"enabled\":false}}"; }

search_once(){
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "$URL/api/v1/search" \
    -H 'Content-Type: application/json' \
    -d '{"query":"договор отчёт платёж система","limit":10,"query_type":"hybrid"}' 2>/dev/null
}

echo -e "${B}SMOKE: пользовательский путь в сжатом виде${N}"
echo "  URL:    $URL"
echo "  папка:  $DIR"
echo "  корпус: $FILES файлов, бюджет ~$MINUTES мин"
echo "  лог:    $LOG"

command -v curl >/dev/null || { echo "нет curl"; exit 2; }
command -v python3 >/dev/null || { echo "нет python3"; exit 2; }

# ── 0. предпосылки ────────────────────────────────────────────────────────────
hdr "0. Предпосылки"
if [ "$(id -u)" = "0" ]; then
  bad "скрипт запущен под root — файлы получат права root, индексатор их не прочитает"
  inf "запусти БЕЗ sudo, от того же пользователя, что и main.py"
fi
MPID=$(pgrep -f "main\.py" 2>/dev/null | xargs -r ps -o pid=,rss= -p 2>/dev/null | sort -k2 -n | tail -1 | awk '{print $1}')
if [ -n "${MPID:-}" ]; then
  MUSER=$(ps -o user= -p "$MPID" 2>/dev/null | xargs)
  ok "индексатор запущен: PID $MPID (пользователь '$MUSER')"
else
  inf "процесс main.py не найден — проверю только по API"
fi

# ── 1. сброс индекса и очереди ────────────────────────────────────────────────
hdr "1. Сброс индекса и очереди"
UHOME="$(getent passwd "${SUDO_USER:-$(id -un)}" 2>/dev/null | cut -d: -f6)"; [ -z "$UHOME" ] && UHOME="$HOME"
STORE="$UHOME/.files-search"
if [ "$NO_RESET" = "1" ]; then
  inf "NO_RESET=1 — сброс пропущен (возможен 'nothing new' и краш LSM)"
else
  inf "останавливаю индексатор..."
  pkill -f "lib-local-rag.*main\.py" 2>/dev/null || pkill -f "main\.py" 2>/dev/null || true
  sleep 3
  rm -rf "$STORE" 2>/dev/null || true
  rm -f "$DIR"/smoke_*.docx "$DIR"/smoke_*.txt "$DIR"/smoke_*.pdf 2>/dev/null || true
  if [ -d "$STORE" ]; then
    bad "не удалось удалить $STORE — держит процесс?"
  else
    ok "индекс и очередь удалены"
  fi
  inf "поднимаю индексатор заново..."
  if [ -f "$INDEXER_DIR/.venv/bin/activate" ]; then
    setsid bash -c "cd '$INDEXER_DIR' && source .venv/bin/activate && python main.py" \
      > "$OUT/indexer_stdout.log" 2>&1 &
  else
    setsid bash -c "cd '$INDEXER_DIR' && python main.py" > "$OUT/indexer_stdout.log" 2>&1 &
  fi
  w=0
  until alive; do
    sleep 2; w=$((w+2))
    [ $((w % 20)) -eq 0 ] && inf "жду health (${w}с)..."
    if [ "$w" -ge 120 ]; then
      bad "индексатор не поднялся за 120с. Лог: $OUT/indexer_stdout.log"
      tail -n 12 "$OUT/indexer_stdout.log" 2>/dev/null | sed 's/^/      /'
      exit 1
    fi
  done
  ok "индексатор поднялся (health 200)"
fi

alive && ok "health 200" || { bad "health не отвечает — запусти main.py"; exit 1; }

# ── 2. корпус ─────────────────────────────────────────────────────────────────
hdr "2. Генерация корпуса ($FILES файлов: pdf + docx + txt)"
mkdir -p "$DIR"
python3 - "$DIR" "$FILES" <<'PYEOF'
import sys, zipfile, random, io
d, n = sys.argv[1], int(sys.argv[2])
W=("договор акт отчёт заявка клиент платёж счёт реквизиты система сервер модуль "
   "конфигурация процесс память ядро планировщик нагрузка индексация документ файл "
   "поиск запрос результат метрика анализ производительность оптимизация ошибка").split()
rnd=random.Random(2026)
def paras(k): return [(" ".join(rnd.choice(W) for _ in range(45))+".").capitalize() for _ in range(k)]
def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def docx(p,ps):
    body="".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>'%esc(x) for x in ps)
    doc='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>%s<w:sectPr/></w:body></w:document>'%body
    ct='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    with zipfile.ZipFile(p,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",ct); z.writestr("_rels/.rels",rels); z.writestr("word/document.xml",doc)
def pdf(p,ps):
    def wrap(t,w=95):
        out,line=[],""
        for word in t.split():
            if len(line)+len(word)+1>w: out.append(line); line=word
            else: line=(line+" "+word).strip()
        if line: out.append(line)
        return out
    lines=[]
    for x in ps:
        lines+= wrap("".join(ch if ord(ch)<128 else "x" for ch in x)); lines.append("")
    sl=["BT","/F1 11 Tf","13 TL","50 780 Td"]
    for l in lines[:52]: sl.append("(%s) Tj T*"%l.replace("\\","\\\\").replace("(","\\(").replace(")","\\)"))
    sl.append("ET"); stream="\n".join(sl).encode("latin-1","replace")
    objs=[b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
          b"2 0 obj<</Type/Pages/Kids[4 0 R]/Count 1>>endobj\n",
          b"3 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
          b"4 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 3 0 R>>>>/Contents 5 0 R>>endobj\n",
          b"5 0 obj<</Length "+str(len(stream)).encode()+b">>stream\n"+stream+b"\nendstream endobj\n"]
    buf=io.BytesIO(); buf.write(b"%PDF-1.4\n"); offs=[]
    for o in objs: offs.append(buf.tell()); buf.write(o)
    xr=buf.tell(); buf.write(b"xref\n0 6\n0000000000 65535 f \n")
    for o in offs: buf.write(("%010d 00000 n \n"%o).encode())
    buf.write(b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n"+str(xr).encode()+b"\n%%EOF")
    open(p,"wb").write(buf.getvalue())
c={"pdf":0,"docx":0,"txt":0}
for i in range(n):
    r=rnd.random(); k=rnd.randint(20,60)
    if r<0.40: pdf(f"{d}/smoke_{i:05d}.pdf", paras(k)); c["pdf"]+=1
    elif r<0.75: docx(f"{d}/smoke_{i:05d}.docx", paras(k)); c["docx"]+=1
    else: open(f"{d}/smoke_{i:05d}.txt","w",encoding="utf-8").write("\n\n".join(paras(k))); c["txt"]+=1
print(f"  pdf={c['pdf']} docx={c['docx']} txt={c['txt']}")
PYEOF
chmod 666 "$DIR"/smoke_* 2>/dev/null || true
ok "корпус готов в $DIR"

# ── 3. POST /config + контроль, что сервис пережил ────────────────────────────
hdr "3. POST /api/v1/config (запуск индексации)"
code=$(curl -s -o "$OUT/config_resp.json" -w '%{http_code}' --max-time 25 \
       -X POST "$URL/api/v1/config" -H 'Content-Type: application/json' -d "$(cfg_json)" 2>/dev/null)
V_CONFIG="$code"
if [ "$code" = "200" ]; then ok "config 200"; else bad "config $code"; head -c 200 "$OUT/config_resp.json" 2>/dev/null; fi
sleep 3
if ! alive; then
  bad "ИНДЕКСАТОР УПАЛ сразу после POST /config"
  V_ALIVE="crashed"; V_CRASH_AT="после POST /config"
  tail -n 6 "$OUT/indexer_stdout.log" 2>/dev/null | sed 's/^/      /'
else
  ok "сервис жив после переконфигурации"
fi

# ── 4. индексация + ОДНОВРЕМЕННЫЙ поиск (главный сценарий) ────────────────────
hdr "4. Индексация + поиск параллельно (главный сценарий пользователя)"
DEADLINE=$(( $(date +%s) + MINUTES*60 ))
REINDEX_DONE=0
last_note=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if ! alive; then
    bad "ИНДЕКСАТОР УПАЛ во время индексации/поиска"
    V_ALIVE="crashed"; [ -z "$V_CRASH_AT" ] && V_CRASH_AT="во время фазы поиска"
    break
  fi
  st=$(status_raw)
  idx=$(jnum "$st" files_indexed); act=$(jbool "$st" is_indexing)
  [ -n "${idx:-}" ] && [ "${idx:-0}" -gt "$V_INDEXED" ] 2>/dev/null && V_INDEXED="$idx"

  sc=$(search_once)
  if [ "$sc" = "200" ]; then V_SEARCH_OK=$((V_SEARCH_OK+1))
  else
    V_SEARCH_ERR=$((V_SEARCH_ERR+1))
    bad "поиск вернул $sc"
  fi

  # доиндексация в середине бюджета
  if [ "$REINDEX_DONE" = "0" ] && [ "$(date +%s)" -gt $(( DEADLINE - MINUTES*30 )) ]; then
    inf "доиндексация: добавляю $REINDEX_FILES новых файлов"
    python3 - "$DIR" "$REINDEX_FILES" <<'PYEOF'
import sys, zipfile, random
d,n=sys.argv[1],int(sys.argv[2])
W="договор отчёт платёж система поиск документ индексация нагрузка".split()
rnd=random.Random(7)
def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
for i in range(n):
    ps=[(" ".join(rnd.choice(W) for _ in range(45))+".").capitalize() for _ in range(30)]
    body="".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>'%esc(x) for x in ps)
    doc='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>%s<w:sectPr/></w:body></w:document>'%body
    ct='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    with zipfile.ZipFile(f"{d}/smoke_new_{i:03d}.docx","w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",ct); z.writestr("_rels/.rels",rels); z.writestr("word/document.xml",doc)
PYEOF
    chmod 666 "$DIR"/smoke_new_* 2>/dev/null || true
    REINDEX_DONE=1
  fi

  now=$(date +%s)
  if [ $(( now - last_note )) -ge 30 ]; then
    left=$(( (DEADLINE - now) / 60 ))
    inf "indexed=${idx:-?} is_indexing=${act:-?} | поиск ok=$V_SEARCH_OK err=$V_SEARCH_ERR | осталось ~${left}м"
    last_note=$now
  fi
  sleep "$SEARCH_EVERY"
done

# ── 5. проверка доиндексации ──────────────────────────────────────────────────
hdr "5. Доиндексация новых файлов"
if [ "$REINDEX_DONE" = "1" ] && alive; then
  sleep 5
  st=$(status_raw); idx2=$(jnum "$st" files_indexed)
  if [ -n "${idx2:-}" ]; then inf "статус после доиндексации: files_indexed=$idx2"; fi
  # ищем маркер: новые docx должны быть найдены
  hits=$(curl -s --max-time 15 -X POST "$URL/api/v1/search" -H 'Content-Type: application/json' \
        -d '{"query":"индексация нагрузка документ","limit":20,"query_type":"hybrid"}' 2>/dev/null \
        | grep -oE '"absolute_path"[ ]*:[ ]*"[^"]+"' | grep -c 'smoke_new_' || echo 0)
  if [ "${hits:-0}" -gt 0 ] 2>/dev/null; then
    V_REINDEX="ok"; ok "новые файлы попали в индекс (найдено: $hits)"
  else
    V_REINDEX="не подтверждено"
    inf "новые файлы пока не найдены поиском (может не успеть — это не всегда ошибка)"
  fi
else
  V_REINDEX="пропущено"
fi

alive && { [ "$V_ALIVE" != "crashed" ] && V_ALIVE="alive"; } || V_ALIVE="crashed"

# ── 6. ВЕРДИКТ ────────────────────────────────────────────────────────────────
hdr "6. Результат"
total=$(( V_SEARCH_OK + V_SEARCH_ERR ))
errpct=0; [ "$total" -gt 0 ] && errpct=$(( V_SEARCH_ERR * 100 / total ))
log "  config:        $V_CONFIG"
log "  проиндексировано (пик files_indexed): $V_INDEXED"
log "  поиск:         всего $total, ok=$V_SEARCH_OK, ошибок=$V_SEARCH_ERR (${errpct}%)"
log "  доиндексация:  $V_REINDEX"
log "  сервис:        $V_ALIVE ${V_CRASH_AT:+($V_CRASH_AT)}"

echo ""
echo -e "${B}══════════════════════════════════════════════${N}"
if [ "$V_ALIVE" = "crashed" ]; then
  echo -e "  ${R}${B}НЕ РАБОТАЕТ${N} — индексатор упал ${V_CRASH_AT}."
  echo -e "  ${Y}Смотри $OUT/indexer_stdout.log — вероятен assert SQLite LSM.${N}"
  echo -e "  ${Y}Полноценный час запускать НЕ стоит: упадёт так же.${N}"
  rc=1
elif [ "$V_CONFIG" != "200" ]; then
  echo -e "  ${R}${B}НЕ РАБОТАЕТ${N} — POST /config вернул $V_CONFIG."
  rc=1
elif [ "$errpct" -gt 5 ]; then
  echo -e "  ${R}${B}ЕСТЬ ПРОБЛЕМЫ${N} — ${errpct}% поисковых запросов с ошибкой."
  rc=1
elif [ "$V_INDEXED" -eq 0 ] 2>/dev/null; then
  echo -e "  ${Y}${B}СОМНИТЕЛЬНО${N} — индексация не зафиксирована (files_indexed=0)."
  echo -e "  ${Y}Счётчик сбрасывается в idle; проверь $OUT/indexer_stdout.log${N}"
  rc=1
else
  echo -e "  ${G}${B}РАБОТАЕТ${N} — сервис жив, индексация прошла, поиск без ошибок."
  echo -e "  ${G}Можно запускать полный тест: sudo ./test_sber_indexer.sh -> 3${N}"
  rc=0
fi
echo -e "${B}══════════════════════════════════════════════${N}"
echo -e "  Лог: ${B}$LOG${N}"
echo -e "  Лог индексатора: ${B}$OUT/indexer_stdout.log${N}"

# уборка корпуса
rm -f "$DIR"/smoke_*.docx "$DIR"/smoke_*.txt "$DIR"/smoke_*.pdf "$DIR"/smoke_new_*.docx 2>/dev/null || true
inf "тестовые файлы удалены из $DIR"
exit "$rc"
