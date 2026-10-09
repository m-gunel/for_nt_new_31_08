import datetime
import pickle
import re
from collections import defaultdict
from email.message import Message
from pathlib import Path

import networkx as nx
from bs4 import BeautifulSoup
from rich.progress import Progress

from src.thunderbird.lazy_iter_mbox import _iter_emails
from src.thunderbird.read_messages import extract_unique_body

_patt_clean = re.compile(r"\n{2,}", re.MULTILINE | re.DOTALL)


def get_charset(body: str):
    soup = BeautifulSoup(body)
    if soup.head and soup.head.find("meta", charset=True):
        charset_value = soup.head.find("meta", charset=True)["charset"]
        return charset_value


def _get_part(part: Message) -> str | None:
    ctype = part.get_content_type()
    cdispo = str(part.get("Content-Disposition") or "")
    if (
        ctype
        in {
            "text/plain",
            "text/html",
        }
        and "attachment" not in cdispo
    ):
        charset = part.get_content_charset()
        body: str = part.get_payload(decode=True).decode(encoding=charset)  # type: ignore
        if ctype == "text/html":
            body = BeautifulSoup(body).get_text()
        if body is not None:
            body = _patt_clean.sub("\n\n", body.strip())
            body = extract_unique_body(body).strip()
        return body


def get_body(message: Message, encoding: str = "utf-8") -> str | None:
    if message.is_multipart():
        for part in message.walk():
            if body := _get_part(part):
                return body
    elif body := _get_part(message):
        return body


# Подставьте свой профиль или найдите его через src.thunderbird.find_files.
_MAIL_ROOT = Path(
    "~/.thunderbird/<профиль>/ImapMail/imap.sberbank.ru"
).expanduser()
sent = str(_MAIL_ROOT / "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-")
inbox = str(_MAIL_ROOT / "INBOX")


ret = defaultdict(list)
graph = nx.Graph()

with Progress() as prog:
    for pp, offset in [(sent, 1270756560), (inbox, 5279436219)]:
        for i, (msg, (d, start, stop)) in enumerate(
            _iter_emails(
                pp,
                progress=prog,
                min_date=datetime.datetime(2025, 8, 1),
                headersonly=True,
                start_offset=offset,
            )
        ):
            if thId := msg["Thread-Index"]:
                body = get_body(msg)
                ret[thId].append((d, (body, msg.keys())))
            if (reply_to := msg["In-Reply-To"]) and (msg_id := msg["Message-ID"]):
                graph.add_edge(msg_id, reply_to)


with open("data/graph.pickle", "bw") as f:
    pickle.dump(graph, f)
