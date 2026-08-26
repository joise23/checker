from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "ozon_checker.sqlite3"
SOURCE_BASE_PATH = DATA_DIR / "ozon_rules_source_base.json"
MAX_BODY_SIZE = 10 * 1024 * 1024


# These are the first rules formalised from the supplied documents. The full source
# corpus is also stored in SQLite and is searched before the model is asked to decide.
CORE_RULES = [
    {
        "code": "CARD-SEARCH-KEYWORDS",
        "scope": "card",
        "title": "Поисковые слова в аннотации",
        "rule_text": "Нарушение, если в описании перечислено подряд более пяти синонимов товара или множество брендов без естественного описания товара.",
        "result_text": "Аннотация содержит поисковые слова.",
        "source_file": "Общая инструкция.docx",
        "source_ref": "P0172-P0174",
    },
    {
        "code": "CARD-SIMA-LAND-IMAGE",
        "scope": "card",
        "title": "Логотип Sima Land на изображении",
        "rule_text": "Логотип Sima Land на фото допустим, только если атрибут Бренд заполнен значением Sima land или Страна карнавалия.",
        "result_text": "Информация в карточке товара противоречит друг другу.",
        "source_file": "Общая инструкция.docx",
        "source_ref": "P0325",
    },
    {
        "code": "CARD-AGE-LABEL",
        "scope": "card",
        "title": "Возрастное ограничение",
        "rule_text": "При указании 18+ или 21+ в карточке признак 18+ не может быть не указан или иметь значение нет/false, кроме перечисленных в инструкции исключений.",
        "result_text": "Для карточки с возрастным ограничением не указана метка 18+.",
        "source_file": "Общая инструкция.docx",
        "source_ref": "P0250-P0260",
    },
    {
        "code": "CARD-EXTERNAL-CONTACTS",
        "scope": "card",
        "title": "Сторонние контакты и ссылки",
        "rule_text": "В карточке нельзя указывать сторонние сайты, телефоны, контакты социальных сетей, адреса электронной почты или призывать перейти на стороннюю площадку; применяются только явно указанные исключения.",
        "result_text": "В карточке указаны сторонние контакты или ссылка.",
        "source_file": "Общая инструкция.docx",
        "source_ref": "P0280-P0284",
    },
    {
        "code": "CARD-REVIEW-REWARD",
        "scope": "card",
        "title": "Вознаграждение за отзыв",
        "rule_text": "Нельзя предлагать скидки, выплаты, подарки или иные условия за положительные оценки и отзывы. Это также относится к принципам честной конкуренции Кодекса продавца.",
        "result_text": "Карточка предлагает вознаграждение за отзыв или оценку.",
        "source_file": "Кодекс продавца маркетплейса Ozon.pdf",
        "source_ref": "PAGE-3",
    },
    {
        "code": "CARD-OFFSITE-OFFER",
        "scope": "card",
        "title": "Побуждение к покупке вне Ozon",
        "rule_text": "Нельзя предлагать скидки, подарки или иные особые условия при покупке на сторонних сайтах, а также побуждать покупателей совершать покупку на другом сайте.",
        "result_text": "Карточка побуждает к покупке на стороннем сайте.",
        "source_file": "Кодекс продавца маркетплейса Ozon.pdf",
        "source_ref": "PAGE-1, PAGE-3",
    },
    {
        "code": "CARD-ACCURACY",
        "scope": "card",
        "title": "Достоверность карточки",
        "rule_text": "Название и описание не должны вводить покупателя в заблуждение о наличии, свойствах, составе, последствиях использования или применения товара.",
        "result_text": "Информация в карточке товара противоречит друг другу.",
        "source_file": "Кодекс продавца маркетплейса Ozon.pdf",
        "source_ref": "PAGE-2",
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialise_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY,
                file_name TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_fragments (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES sources(id),
                source_ref TEXT NOT NULL,
                fragment_type TEXT NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(source_id, source_ref, fragment_type)
            );
            CREATE TABLE IF NOT EXISTS rules (
                code TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                title TEXT NOT NULL,
                rule_text TEXT NOT NULL,
                result_text TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_ref TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            """
        )
        for rule in CORE_RULES:
            connection.execute(
                """INSERT INTO rules(code, scope, title, rule_text, result_text, source_file, source_ref)
                   VALUES(:code, :scope, :title, :rule_text, :result_text, :source_file, :source_ref)
                   ON CONFLICT(code) DO UPDATE SET scope=excluded.scope, title=excluded.title,
                   rule_text=excluded.rule_text, result_text=excluded.result_text,
                   source_file=excluded.source_file, source_ref=excluded.source_ref""",
                rule,
            )
    import_source_base()


def import_source_base() -> None:
    if not SOURCE_BASE_PATH.exists():
        return
    payload = json.loads(SOURCE_BASE_PATH.read_text(encoding="utf-8"))
    with db() as connection:
        for document in payload.get("documents", []):
            file_name = document["source_file"]
            source_type = document.get("source_type", "docx")
            connection.execute(
                """INSERT INTO sources(file_name, source_type, imported_at) VALUES (?, ?, ?)
                   ON CONFLICT(file_name) DO UPDATE SET source_type=excluded.source_type""",
                (file_name, source_type, utc_now()),
            )
            source_id = connection.execute("SELECT id FROM sources WHERE file_name=?", (file_name,)).fetchone()[0]
            for paragraph in document.get("paragraphs", []):
                connection.execute(
                    """INSERT INTO source_fragments(source_id, source_ref, fragment_type, content)
                       VALUES (?, ?, 'paragraph', ?)
                       ON CONFLICT(source_id, source_ref, fragment_type) DO UPDATE SET content=excluded.content""",
                    (source_id, paragraph["source_ref"], paragraph["text"]),
                )
            for table in document.get("tables", []):
                for row in table.get("rows", []):
                    content = " | ".join(cell for cell in row["cells"] if cell)
                    if content:
                        connection.execute(
                            """INSERT INTO source_fragments(source_id, source_ref, fragment_type, content)
                               VALUES (?, ?, 'table_row', ?)
                               ON CONFLICT(source_id, source_ref, fragment_type) DO UPDATE SET content=excluded.content""",
                            (source_id, row["source_ref"], content),
                        )
            for page in document.get("pages", []):
                if page.get("text"):
                    connection.execute(
                        """INSERT INTO source_fragments(source_id, source_ref, fragment_type, content)
                           VALUES (?, ?, 'page', ?)
                           ON CONFLICT(source_id, source_ref, fragment_type) DO UPDATE SET content=excluded.content""",
                        (source_id, page["source_ref"], page["text"]),
                    )


def normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().replace("ё", "е")).strip()


def listing_text(card: dict[str, Any]) -> str:
    parts = [str(card.get(key, "")) for key in ("category", "group", "title", "brand", "annotation")]
    for item in card.get("attributes", []):
        parts.extend([str(item.get("key", "")), str(item.get("value", "")), str(item.get("unit", ""))])
    return "\n".join(part for part in parts if part)


def source_reference(rule: dict[str, str]) -> dict[str, str]:
    return {"rule_code": rule["code"], "source": f"{rule['source_file']} · {rule['source_ref']}"}


def keyword_spam(annotation: str, title: str) -> bool:
    lines = [normalise(line) for line in annotation.splitlines() if line.strip()]
    if not lines:
        return False
    tail = lines[-1]
    if len(tail) < 120 or len(re.findall(r"[а-яa-z]{3,}", tail)) < 25:
        return False
    title_words = {word[:6] for word in re.findall(r"[а-яa-z]{4,}", normalise(title))}
    tail_words = [word[:6] for word in re.findall(r"[а-яa-z]{4,}", tail)]
    return sum(tail_words.count(word) >= 3 for word in title_words) >= 2


def deterministic_checks(card: dict[str, Any]) -> list[dict[str, Any]]:
    rules = {row["code"]: dict(row) for row in db().execute("SELECT * FROM rules")}
    text = normalise(listing_text(card))
    annotation = str(card.get("annotation", ""))
    title = str(card.get("title", ""))
    age_flag = normalise(str(card.get("age_18", "")))
    violations: list[dict[str, Any]] = []

    def add(code: str, evidence: str) -> None:
        rule = rules[code]
        if not any(item["rule_code"] == code for item in violations):
            violations.append({"text": rule["result_text"], "evidence": evidence, **source_reference(rule)})

    if keyword_spam(annotation, title):
        add("CARD-SEARCH-KEYWORDS", "В аннотации есть длинное повторяющееся перечисление вариантов товара.")
    if re.search(r"\b(?:18|21)\s*\+", text) and age_flag not in {"да", "true", "1", "yes"}:
        add("CARD-AGE-LABEL", "В тексте карточки указано возрастное ограничение, а метка 18+ не установлена.")
    reward_words = ("скидк", "подар", "кешбэк", "кэшбэк", "бонус", "балл", "деньг", "выплат")
    review_words = ("за отзыв", "за оценк", "положительн отзыв", "оставь отзыв")
    if any(word in text for word in review_words) and any(word in text for word in reward_words):
        add("CARD-REVIEW-REWARD", "В карточке одновременно есть упоминания отзыва/оценки и вознаграждения.")
    if ("купите на" in text or "покупк" in text and "сайте" in text) and re.search(r"(?:https?://|www\.|\b[\w-]+\.(?:ru|com|рф|su|me)\b)", text):
        add("CARD-OFFSITE-OFFER", "В карточке есть побуждение к покупке и сторонний сайт.")
    if re.search(r"(?:https?://|www\.|\b[\w-]+\.(?:ru|com|рф|su|me)\b|\+?\d[\d\s()\-]{8,}\b|\b[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}\b)", text):
        add("CARD-EXTERNAL-CONTACTS", "В текстовых полях обнаружен сторонний контакт или ссылка.")
    return violations


def search_context(card: dict[str, Any], limit: int = 12) -> list[dict[str, str]]:
    tokens = [token for token in re.findall(r"[а-яa-z0-9]{4,}", normalise(listing_text(card)))][:70]
    if not tokens:
        return []
    score: dict[int, int] = {}
    rows: dict[int, sqlite3.Row] = {}
    with db() as connection:
        for token in set(tokens):
            for row in connection.execute(
                """SELECT f.id, f.source_ref, f.content, s.file_name FROM source_fragments f
                   JOIN sources s ON s.id=f.source_id WHERE lower(f.content) LIKE ? LIMIT 50""",
                (f"%{token}%",),
            ):
                rows[row["id"]] = row
                score[row["id"]] = score.get(row["id"], 0) + 1
    ranked = sorted(rows.values(), key=lambda row: score[row["id"]], reverse=True)[:limit]
    return [
        {"source": f"{row['file_name']} · {row['source_ref']}", "text": row["content"][:1800]}
        for row in ranked
    ]


def extract_output_text(response: dict[str, Any]) -> str:
    for choice in response.get("choices", []):
        content = choice.get("message", {}).get("content", "")
        if isinstance(content, str):
            return content
    raise ValueError("Провайдер не вернул текстовый ответ.")


def llm_check(card: dict[str, Any], context: list[dict[str, str]]) -> dict[str, Any] | None:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    raw_base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    if not api_key or not model:
        return None

    if raw_base_url.endswith("/chat/completions"):
        endpoint_url = raw_base_url
    else:
        endpoint_url = f"{raw_base_url}/chat/completions"
    core_rules = [dict(row) for row in db().execute("SELECT * FROM rules WHERE scope='card'")]
    compact_card = {key: card.get(key, "") for key in ("category", "group", "title", "brand", "annotation", "age_18")}
    compact_card["attributes"] = card.get("attributes", [])
    prompt = {
        "task": "Проверь карточку товара по переданным правилам. Нельзя придумывать правила или нарушения. Если в карточке содержится несколько нарушений, обязательно выяви и перечисли ВСЕ ошибки и нарушения в массиве violations.",
        "decision_policy": {
            "violation": "Есть конкретное правило и конкретное подтверждение в тексте или на фото. Перечисли абсолютно ВСЕ найденные нарушения.",
            "clean": "Все применимые правила можно проверить по этим данным, нарушений нет.",
            "requires_review": "Данных недостаточно, правило неоднозначно или проверка требует внешнего реестра/документа.",
        },
        "card": compact_card,
        "formalised_rules": core_rules,
        "retrieved_source_fragments": context,
        "response_schema": {
            "status": "violation | clean | requires_review",
            "violations": [{"text": "короткая формулировка нарушения", "rule_code": "код правила или SOURCE", "evidence": "точный фрагмент карточки"}],
            "review_reason": "строка или пустая строка",
        },
    }
    prompt_str = json.dumps(prompt, ensure_ascii=False)
    image = card.get("image_data_url", "")
    if isinstance(image, str) and image.startswith("data:image/"):
        user_content: str | list[dict[str, Any]] = [
            {"type": "text", "text": prompt_str},
            {"type": "image_url", "image_url": {"url": image, "detail": "low"}},
        ]
    else:
        user_content = prompt_str

    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Отвечай только корректным JSON по указанной схеме."},
                {"role": "user", "content": user_content},
            ],
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-goog-api-key": api_key,
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(
        endpoint_url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        model_result = json.loads(extract_output_text(payload))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        print(f"[LLM ERROR] HTTP {exc.code}: {err_body}")
        return {"status": "requires_review", "violations": [], "review_reason": f"Не удалось выполнить LLM-проверку (HTTP {exc.code}): {err_body or exc.reason}"}
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"[LLM ERROR] {exc}")
        return {"status": "requires_review", "violations": [], "review_reason": f"Не удалось выполнить LLM-проверку: {exc}"}
    if model_result.get("status") not in {"violation", "clean", "requires_review"}:
        return {"status": "requires_review", "violations": [], "review_reason": "LLM вернула неизвестный статус."}
    return model_result


def check_card(card: dict[str, Any]) -> dict[str, Any]:
    context = search_context(card)
    deterministic = deterministic_checks(card)
    model_result = llm_check(card, context)
    violations = deterministic[:]
    if model_result:
        for violation in model_result.get("violations", []):
            if isinstance(violation, dict) and violation.get("text"):
                code = str(violation.get("rule_code", "SOURCE"))
                text = str(violation["text"])
                evidence = str(violation.get("evidence", ""))
                fingerprint = (code, normalise(text), normalise(evidence))
                existing_fingerprints = {
                    (item.get("rule_code", ""), normalise(str(item.get("text", ""))), normalise(str(item.get("evidence", ""))))
                    for item in violations
                }
                if fingerprint not in existing_fingerprints:
                    violations.append(
                        {
                            "text": text,
                            "rule_code": code,
                            "evidence": evidence,
                            "source": "Проверка LLM по загруженным источникам",
                        }
                    )
    if violations:
        status = "violation"
        if model_result and model_result.get("review_reason"):
            review_reason = str(model_result["review_reason"])
        else:
            review_reason = ""
    elif model_result:
        status = model_result["status"]
        review_reason = str(model_result.get("review_reason", ""))
    else:
        status = "requires_review"
        review_reason = "LLM-проверка не настроена. Проверены только формализованные правила."
    result = {
        "status": status,
        "violations": violations,
        "review_reason": review_reason,
        "mode": "rules+llm" if model_result else "rules_only",
        "source_context": [{"source": item["source"]} for item in context],
    }
    stored_card = {key: value for key, value in card.items() if key != "image_data_url"}
    stored_card["image_present"] = bool(card.get("image_data_url"))
    with db() as connection:
        connection.execute(
            "INSERT INTO checks(created_at, status, input_json, result_json) VALUES (?, ?, ?, ?)",
            (utc_now(), status, json.dumps(stored_card, ensure_ascii=False), json.dumps(result, ensure_ascii=False)),
        )
    return result


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            with db() as connection:
                source_count = connection.execute("SELECT count(*) FROM sources").fetchone()[0]
                fragment_count = connection.execute("SELECT count(*) FROM source_fragments").fetchone()[0]
                rule_count = connection.execute("SELECT count(*) FROM rules").fetchone()[0]
            self.send_json(
                {
                    "sources": source_count,
                    "fragments": fragment_count,
                    "rules": rule_count,
                    "llm_configured": bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL")),
                }
            )
            return
        if self.path == "/api/history":
            with db() as connection:
                rows = connection.execute(
                    "SELECT id, created_at, status, input_json, result_json FROM checks ORDER BY id DESC LIMIT 20"
                ).fetchall()
            self.send_json(
                [
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "status": row["status"],
                        "card": json.loads(row["input_json"]),
                        "result": json.loads(row["result_json"]),
                    }
                    for row in rows
                ]
            )
            return
        if self.path == "/api/rules":
            with db() as connection:
                rows = connection.execute("SELECT * FROM rules ORDER BY code").fetchall()
            self.send_json([dict(row) for row in rows])
            return
        if self.path in {"/", "/index.html"}:
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/check":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > MAX_BODY_SIZE:
            self.send_json({"error": "Некорректный или слишком большой запрос."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            card = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not str(card.get("title", "")).strip():
                raise ValueError("Заполните название товара.")
            if card.get("image_data_url"):
                base64.b64decode(str(card["image_data_url"]).split(",", 1)[-1], validate=True)
            self.send_json(check_card(card))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


if __name__ == "__main__":
    initialise_database()
    host = os.getenv("OZON_CHECKER_HOST", "127.0.0.1")
    port = int(os.getenv("OZON_CHECKER_PORT", "8080"))
    print(f"Ozon Card Checker: http://{host}:{port}")
    ThreadingHTTPServer((host, port), AppHandler).serve_forever()
