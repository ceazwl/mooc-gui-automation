"""Answer quiz questions by calling an LLM API directly -- the real decision layer.

Why this exists: the first version answered quizzes with a keyword rule plus a
hand-written 10-entry lookup table and then waited for a human for anything else.
That is backwards -- the model should be asked first, and the table should only be
a cache. Layers, in order:

  1. llm_answer()   -- ask the model (this file)
  2. known answers  -- cache of questions already answered CORRECTLY
  3. help channel   -- park and ask the operator, only if the model cannot answer

There is no local rule layer: every heuristic that ever answered a question here
(an absolutist-phrase table, a longest-option guess) answered some of them wrong,
and a wrong answer is no longer silently accepted -- the platform's verdict is
checked, and a refused letter is fed back so the retry cannot repeat it.

Provider: DeepSeek (OpenAI-compatible). The key is read from the DSH credential
store so no secret is written into the project.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """你是一个严谨的答题助手，负责学术诚信与工程伦理课程。

用户会给你一道题的题干、选项，以及平台已经判定为错误的选项（可能为空）。

要求：
1. 只输出 JSON，不要任何解释性文字或 markdown 代码块。
2. 格式：{"answer": "<选项字母>", "confidence": <0-1的小数>, "reason": "<20字以内理由>"}
3. answer 必须是给出的选项字母之一，且绝不能是平台已判定错误的选项。
4. 依据题干与选项本身的内容作答，不要依赖题型的套话或关键词模板
   （例如"绝对化表述就是错"这类规则会答错题）。
5. 不确定时给出把握最大的答案，并用 confidence 表示把握程度。"""


def api_key() -> str | None:
    """The model key: DEEPSEEK_API_KEY, else a one-line file at MOOC_LLM_KEY_FILE.

    Deliberately site-independent: no credential store of any particular tool is read, so
    this file can be handed to someone else unchanged.
    """
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key.strip()
    path = (os.environ.get("MOOC_LLM_KEY_FILE")
            or os.path.join(os.path.expanduser("~"), ".mooc_llm_key"))
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.read().strip().splitlines()[0]
        return first.strip().strip("'\"") or None
    except (OSError, IndexError):
        return None


def _post(url: str, payload: dict, key: str, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def llm_answer(question: str, options: list[dict], kind: str = "判断题",
               reject: tuple = (), timeout: float = 40.0,
               model: str | None = None) -> dict | None:
    """Ask the model to choose an option.

    `reject` lists letters the platform has already marked wrong for this exact
    question. Telling the model is what makes a retry different from the attempt
    that just failed: the same prompt otherwise reproduces the same wrong answer.

    Returns {"answer", "confidence", "reason", "model"} or None on any failure
    (no key, network error, unparseable reply) so the caller can fall back rather
    than crash.
    """
    key = api_key()
    if not key:
        return None

    bad = [str(r).upper() for r in (reject or []) if r]
    opts = "\n".join(
        f"{o.get('letter')}. {o.get('text') or '(no text read)'}" for o in options)
    user = (f"题型：{kind}\n"
            f"题干：{question}\n"
            f"选项：\n{opts}\n")
    if bad:
        user += (f"平台已判定这些选项是错误的，不能再选：{'、'.join(bad)}\n")
    user += "\n请只输出 JSON。"

    base = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE).rstrip("/")
    mdl = model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
    try:
        resp = _post(f"{base}/chat/completions", {
            "model": mdl,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
            "temperature": 0.0,
            "max_tokens": 200,
            "stream": False,
        }, key, timeout)
        content = resp["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        if not parsed:
            return {"answer": None, "raw": content, "model": mdl,
                    "reason": "unparseable reply"}
        ans = str(parsed.get("answer", "")).strip().upper()[:1]
        letters = {str(o.get("letter", "")).upper() for o in options}
        parsed["answer"] = ans if ans in letters else None
        parsed["model"] = mdl
        return parsed
    except urllib.error.HTTPError as e:
        return {"answer": None, "error": f"HTTP {e.code}", "model": mdl}
    except Exception as e:
        return {"answer": None, "error": f"{type(e).__name__}: {e}", "model": mdl}


if __name__ == "__main__":
    # smoke test with real questions from this course
    q = ("很多高科技是探索性的和不成熟的，其技术后果和伦理价值影响难以预见。")
    print("key found:", bool(api_key()))
    print(llm_answer(q, [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}]))
    q2 = ("在科学研究中因粗心大意或一时疏忽而造成差错是难免的，也是可以接受的。")
    # with a refusal: the model must not pick the letter the platform rejected
    print(llm_answer(q2, [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}],
                     reject=("B",)))
