#!/usr/bin/env bash
# check_indexer_fix.sh — проверка готовности индексатора к НТ.
#
# НАДЁЖНЫЙ КРИТЕРИЙ: создаём файлы с УНИКАЛЬНЫМ маркером и ищем его через /search.
# Если маркер находится — индексация точно отработала. Это устойчиво к тому, что
# индексатор после завершения сбрасывает счётчики в idle (files_indexed=0), и к
# тому, что уже проиндексированные файлы он пропускает ("nothing new").
#
#   ./check_indexer_fix.sh            # обычная проверка
#   ./check_indexer_fix.sh --keep     # оставить тестовые файлы
#   URL=... DIR=... WAIT_SECS=90 ./check_indexer_fix.sh
set -u

# Файлы, которые создаёт этот скрипт, должен читать процесс main (работает от
# ПОЛЬЗОВАТЕЛЯ). Если скрипт запущен под sudo/root с umask 077, файлы будут 600
# root:root -> main получит PermissionError. Поэтому открываем права всем.
umask 000

URL="${URL:-http://127.0.0.1:8000}"
DIR="${DIR:-$HOME/index_test_files}"
WAIT_SECS="${WAIT_SECS:-90}"; KEEP=0
for a in "$@"; do case "$a" in
  --keep) KEEP=1 ;;
  -h|--help) grep -E '^#' "$0" | sed 's/^# \{0,1\}//' | head -10; exit 0 ;;
  *) echo "Неизвестный аргумент: $a"; exit 2 ;; esac; done
if [ -t 1 ]; then R=$'\033[0;31m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; C=$'\033[0;36m'; B=$'\033[1m'; N=$'\033[0m'
else R=''; G=''; Y=''; C=''; B=''; N=''; fi
ok(){ echo -e "  ${G}OK${N} $*"; }; bad(){ echo -e "  ${R}!!${N} $*"; }
inf(){ echo -e "  ${C}->${N} $*"; }; hdr(){ echo -e "\n${B}$*${N}"; }

MARK="SBERCHECK$(date +%H%M%S)$RANDOM"
cfg_json(){ printf '%s' "{\"email\":{\"enabled\":false,\"index_last_n_days\":null,\"offsets\":null,\"path_to_email_folder\":\"\"},\"folders\":[\"$DIR\"],\"sberchat\":{\"enabled\":false}}"; }
search_mark(){ curl -s --max-time 15 -X POST "$URL/api/v1/search" -H 'Content-Type: application/json' \
    -d "{\"query\":\"$MARK\",\"limit\":5,\"query_type\":\"hybrid\"}" 2>/dev/null; }

echo -e "${B}Проверка индексатора: $URL${N}"; echo "  папка: $DIR"; echo "  маркер прогона: $MARK"
command -v curl >/dev/null || { bad "curl не найден"; exit 2; }

hdr "0. Предпосылки"
# Владелец процесса main — именно его домашний каталог важен для email root,
# и именно он должен иметь права на чтение тестовых файлов.
MAIN_PID=$(pgrep -f 'lib-local-rag.*main.py|/main$' 2>/dev/null | xargs -r ps -o pid=,rss= -p 2>/dev/null \
           | sort -k2 -n | tail -1 | awk '{print $1}')
MAIN_USER=""
if [ -n "${MAIN_PID:-}" ]; then
  MAIN_USER=$(ps -o user= -p "$MAIN_PID" 2>/dev/null | xargs)
  if [ "$MAIN_USER" = "root" ]; then
    bad "main работает от root -> HOME=/root, email root не найдётся"
  else
    ok "main работает от '$MAIN_USER' (PID $MAIN_PID)"
  fi
else
  inf "процесс main не найден — проверю только по API"
fi
# email root ищем в домашнем каталоге ВЛАДЕЛЬЦА main, а не в $HOME этого скрипта
# ($HOME под sudo = /root, что вводит в заблуждение).
EROOT_USER="${MAIN_USER:-${SUDO_USER:-$(id -un)}}"
EHOME=$(getent passwd "$EROOT_USER" 2>/dev/null | cut -d: -f6)
[ -z "$EHOME" ] && EHOME="$HOME"
if [ -d "$EHOME/.thunderbird" ]; then ok "email root есть: $EHOME/.thunderbird"
else inf "нет $EHOME/.thunderbird — для сборки 2026-08-25 это НЕ блокер"
     inf "(email_files() теперь пишет warning и возвращает [], индексация идёт)"; fi

hdr "1. Health"
hc=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/api/v1/health" 2>/dev/null)
[ "$hc" = "200" ] && ok "health 200" || { bad "health=$hc"; exit 1; }

hdr "2. Тестовые файлы (реалистичные, с маркером $MARK)"
mkdir -p "$DIR" 2>/dev/null
PYBIN="$(command -v python3 || command -v python)"
[ -z "$PYBIN" ] && { bad "python3 не найден — нужен для генерации docx"; exit 2; }
"$PYBIN" - "$DIR" "$MARK" <<'PYEOF'
import sys, zipfile, random
d, mark = sys.argv[1], sys.argv[2]
WORDS = ("договор акт отчёт заявка клиент платёж счёт реквизиты система сервер модуль "
         "конфигурация процесс память ядро планировщик нагрузка индексация документ файл "
         "поиск запрос результат метрика анализ производительность оптимизация ошибка").split()
rnd = random.Random(7)
def paras(n=40):
    out=[]
    for i in range(n):
        w=[rnd.choice(WORDS) for _ in range(45)]
        if i % 3 == 0: w.insert(rnd.randint(0,len(w)), mark)
        s=" ".join(w); out.append(s[0].upper()+s[1:]+".")
    return out
def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def docx(path, ps):
    body="".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % esc(p) for p in ps)
    doc=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
         '<w:body>%s<w:sectPr/></w:body></w:document>' % body)
    ct=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>')
    rels=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",ct); z.writestr("_rels/.rels",rels); z.writestr("word/document.xml",doc)
made=[]
for i in (1,2):
    p=f"{d}/indexcheck_{i}.txt"; open(p,"w",encoding="utf-8").write("\n\n".join(paras()))
    made.append(p)
for i in (1,2):
    p=f"{d}/indexcheck_{i}.docx"; docx(p, paras()); made.append(p)
for p in made:
    import os; print(f"  {os.path.basename(p)}  {os.path.getsize(p)} байт")
PYEOF
chmod 666 "$DIR"/indexcheck_* 2>/dev/null || true
if [ -n "${MAIN_USER:-}" ] && [ "$(id -un)" != "$MAIN_USER" ]; then
    chown "$MAIN_USER" "$DIR"/indexcheck_* 2>/dev/null || true
fi
ok "создано 2 txt + 2 docx (по ~10-20 КБ, с маркером внутри), права 666"
# КОНТРОЛЬ: может ли main (от своего пользователя) реально прочитать файлы?
if [ -n "${MAIN_USER:-}" ] && [ "$(id -un)" != "$MAIN_USER" ]; then
    if sudo -u "$MAIN_USER" test -r "$DIR/indexcheck_1.docx" 2>/dev/null; then
        ok "пользователь '$MAIN_USER' может читать тестовые файлы"
    else
        bad "пользователь '$MAIN_USER' НЕ может прочитать файлы -> будет PermissionError!"
        inf "проверь права на каталог: ls -ld '$DIR'"
    fi
fi

hdr "3. POST /api/v1/config"
r=$(curl -s -w $'\n%{http_code}' --max-time 20 -X POST "$URL/api/v1/config" -H 'Content-Type: application/json' -d "$(cfg_json)" 2>/dev/null)
cfg=$(printf '%s' "$r" | tail -n1); cb=$(printf '%s' "$r" | sed '$d')
if [ "$cfg" = 200 ]; then ok "config 200 — рескан запущен"
else bad "config $cfg"; [ -n "$cb" ] && echo "      $(printf '%s' "$cb" | head -c 300)"; fi

hdr "4. Ждём появления маркера в индексе (до ${WAIT_SECS}с)"
found=0; mx=0; last=""
dl=$(( $(date +%s) + WAIT_SECS ))
while [ "$(date +%s)" -lt "$dl" ]; do
  st=$(curl -s --max-time 5 "$URL/api/v1/status" 2>/dev/null); last="$st"
  i=$(printf '%s' "$st" | grep -oE '"files_indexed"[ ]*:[ ]*[0-9]+' | grep -oE '[0-9]+' | head -1)
  x=$(printf '%s' "$st" | grep -oE '"is_indexing"[ ]*:[ ]*(true|false)' | grep -oE '(true|false)' | head -1)
  if [ -n "$i" ] && [ "$i" -gt "$mx" ] 2>/dev/null; then mx="$i"; fi
  # ГЛАВНЫЙ критерий: среди результатов поиска ЕСТЬ наши файлы indexcheck_*.
  # ВАЖНО: гибридный поиск всегда возвращает ближайших соседей, поэтому
  # "total > 0" ничего не доказывает — проверяем именно пути файлов.
  sb=$(search_mark)
  hits=$(printf '%s' "$sb" | grep -oE '"absolute_path"[ ]*:[ ]*"[^"]+"' | sed 's/.*: *"//; s/"$//')
  mine=$(printf '%s' "$hits" | grep -c 'indexcheck_' 2>/dev/null || echo 0)
  printf "\r    files_indexed=%s is_indexing=%s | наших файлов в выдаче: %s      " "${i:-?}" "${x:-?}" "$mine"
  if [ "$mine" -gt 0 ] 2>/dev/null; then found=1; break; fi
  sleep 3
done
echo ""
inf "последний status: $(printf '%s' "$last" | head -c 160)"
[ "$mx" -gt 0 ] 2>/dev/null && inf "пиковый files_indexed за прогон: $mx (счётчик сбрасывается в idle — не показатель)"

hdr "5. Что именно проиндексировалось"
allhits=$(search_mark | grep -oE '"absolute_path"[ ]*:[ ]*"[^"]+"' | sed 's/.*: *"//; s/"$//')
myhits=$(printf '%s' "$allhits" | grep 'indexcheck_' || true)
if [ "$found" = 1 ] && [ -n "$myhits" ]; then
  ok "НАШИ файлы найдены поиском — новые файлы реально индексируются:"
  printf '%s\n' "$myhits" | while read -r h; do [ -n "$h" ] && echo "      $(basename "$h")"; done
  printf '%s' "$myhits" | grep -q '\.txt'  && ok "txt индексируется"  || inf "txt НЕ попал в индекс (вероятно, не поддерживается)"
  printf '%s' "$myhits" | grep -q '\.docx' && ok "docx индексируется" || inf "docx НЕ попал в индекс"
else
  bad "наших файлов (indexcheck_*) в индексе нет за ${WAIT_SECS}с"
  if [ -n "$allhits" ]; then
    inf "поиск вернул только ПОСТОРОННИЕ файлы (гибридный поиск всегда отдаёт"
    inf "ближайших соседей — это НЕ признак успеха):"
    printf '%s\n' "$allhits" | head -3 | while read -r h; do [ -n "$h" ] && echo "      $(basename "$h")"; done
  fi
fi

echo ""; echo -e "${B}==============================================${N}"
if [ "$cfg" = 200 ] && [ "$found" = 1 ] && [ -n "$myhits" ]; then
  echo -e "  ${G}${B}ГОТОВ К НТ${N} — новые файлы индексируются и находятся поиском."
  echo -e "  ${G}Запускай: sudo ./test_sber_indexer.sh -> пункт 3${N}"; v=0
else
  echo -e "  ${R}${B}НЕ ГОТОВ${N} — config=$cfg, наши файлы в индексе: ${found}"
  echo -e "  ${Y}Смотри консоль main:${N}"
  echo -e "    'PermissionError: [Errno 13]' -> файлы созданы под root, main их не читает."
  echo -e "       Запускай этот скрипт ОТ ПОЛЬЗОВАТЕЛЯ (без sudo), либо проверь права папки."
  echo -e "    'email root not found' -> нужен ~/.thunderbird + запуск main ОТ ПОЛЬЗОВАТЕЛЯ"
  echo -e "    'total=0 / nothing new' -> индексатор считает файлы уже известными:"
  echo -e "       нужен ПОЛНЫЙ СБРОС: останови main, ${BOLD}rm -rf ~/.files-search${NC}${Y}, запусти main"; v=1
fi
echo -e "${B}==============================================${N}"
if [ "$KEEP" = 0 ]; then rm -f "$DIR"/indexcheck_*.txt "$DIR"/indexcheck_*.docx 2>/dev/null; inf "тестовые файлы удалены"
else inf "оставлены: $DIR/indexcheck_*"; fi
exit "$v"
