"""실측 XDR 패턴에서 레코드를 찍어낸다 — **달라지는 부분만 치환한다.**

## 왜 이 방식인가

명세로 조립하면 정답지가 이미 담은 것을 다시 적어야 한다 — 성공
Cause(`2001`) · 하류 EMM 대응 · 해제 사유 분포 · 단계별 발생률. 하나씩
실측해 옮기는 동안 **네 군데가 틀렸고** 그때마다 정답지를 다시 읽어
고쳤다.

패턴은 그 왕복을 없앤다. 실측 레코드를 틀로 쓰므로 —

* 성공 Cause·하류 맵·release 규칙이 **공짜로** 맞는다
* `SPID`(140번)처럼 **정체를 모르는 필드도** 올바른 값이 들어간다
* 조립 로직이 사라져 자원도 덜 쓴다

사용자 지시 2026-10-28: *"정답지 자체를 패턴화 해놓고 달라지는 부분만
계속적으로 바꿔가면 변경해서 쓰면 더 쉽고 자원도 적게 쓰면서 할 수 있을꺼야."*

## 세 가지만 바꾼다

| | 무엇 | 왜 |
| --- | --- | --- |
| **시각** | `call_start` 를 원하는 분에 놓고 나머지를 같은 델타로 민다 | 패턴에 `@상대초` 로 들어 있어 **간격이 실측 그대로** 유지된다 |
| **식별자** | `IMSI`·`MDN`·`IMEI` 를 가입자 풀에서 | 실측 암호문을 저장소에 두지 않는다(추출 때 이미 비웠다) |
| **셀·장비** | eNB·Cell·TAC·VLAN·PLMN·MME/SGW/PGW 를 셀 마스터에서 | 실제 기지국 자산 대장을 쓰지 않는다 |

그 밖의 **148개 필드는 실측 그대로**다.

## 장애는 패턴 위에 덮는다

패턴 자체가 오류 조합을 담고 있다(실측 2.10%). 특정 장애를 원하면
**그 키의 패턴을 고르면 된다** — 조립할 필요가 없다. 실측에 없는
조합(예: S13 단말식별 거부)만 `callflow` 명세로 덮어쓴다.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

#: 패턴 파일. `data/` 는 `.gitignore` 라 **산출물 자리**이고, 이 파일에는
#: 식별자·자산 ID 가 비어 있다(`tools/extract_patterns.WIPE`).
DEFAULT_BANK = Path(__file__).resolve().parents[2] / "data" / "callflow" / "patterns.jsonl"

#: 상대 시각 표시. `tools/extract_patterns` 가 붙인다.
REL = "@"

#: 패턴을 가르는 키 — 추출 쪽과 **같은 순서여야 한다.**
KEY_FIELDS = ("call_type", "attempt_flag", "success_flag",
              "first_error_interface_protocol", "first_error_cause")


class Bank:
    """패턴 묶음. 키로 고르고 실측 구성비로 추첨한다."""

    def __init__(self, path: Path | str | None = None):
        p = Path(path) if path else DEFAULT_BANK
        if not p.exists():
            raise FileNotFoundError(
                f"패턴이 없다: {p}\n"
                "  먼저 뽑는다 — python3 tools/extract_patterns.py")
        self.by_key: dict[tuple, list[dict]] = defaultdict(list)
        self.weight: dict[tuple, int] = {}
        for line in p.open(encoding="utf-8"):
            if not line.strip():
                continue
            d = json.loads(line)
            k = tuple(d["key"])
            self.by_key[k].append(d["rec"])
            self.weight[k] = d["weight"]
        self.keys = sorted(self.by_key)

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_key.values())

    def match(self, *, call_type=None, success=None, iface=None, cause=None):
        """조건에 맞는 키들. `None` 은 **묻지 않는다**는 뜻이다."""
        out = []
        for k in self.keys:
            if call_type is not None and k[0] != str(call_type):
                continue
            if success is not None and k[2] != str(success):
                continue
            if iface is not None and k[3] != str(iface):
                continue
            if cause is not None and k[4] != str(cause):
                continue
            out.append(k)
        return out

    def draw(self, keys, rng: random.Random) -> tuple[tuple, dict]:
        """실측 구성비로 키를 고르고 그 안에서 패턴 하나.

        **구성비를 쓰는 이유**는 불변규칙 3 이다 — Call Type 과 결말의
        비율이 바뀌면 품질이 그대로여도 전체 합산 소통률이 움직인다.
        """
        if not keys:
            raise ValueError("조건에 맞는 패턴이 없다")
        w = [self.weight[k] for k in keys]
        k = rng.choices(keys, weights=w, k=1)[0]
        return k, rng.choice(self.by_key[k])


def stamp(pat: dict, t0: float) -> dict:
    """상대 시각을 ``t0`` 에 놓는다. **간격은 실측 그대로다.**"""
    out = {}
    for k, v in pat.items():
        if isinstance(v, str) and v.startswith(REL):
            try:
                out[k] = f"{t0 + float(v[1:]):.6f}"
                continue
            except ValueError:
                pass
        out[k] = v
    return out


#: `(패턴 필드, 셀 속성)` — 셀 마스터에서 그대로 가져온다.
#: **`First_TAC`·`Last_TAC`·`*_eNB_C_UID` 는 없다.** 통합 규격의 TAC 는
#: `interval_First_TAC` 와 `old_call_Last_TAC` 뿐이다 — First/Last 쌍이
#: 있을 것이라 짐작하고 적었다가 `field_names()` 대조에서 걸렸다.
CELL_MAP = (
    ("First_eNB_ID", "enb_id"), ("First_Cell_ID", "cell_id"),
    ("First_eNB_VLAN_ID", "vlan_id"), ("First_eNB_PLMN", "plmn"),
    ("Last_eNB_ID", "enb_id"), ("Last_Cell_ID", "cell_id"),
    ("Last_eNB_VLAN_ID", "vlan_id"), ("Last_eNB_PLMN", "plmn"),
    ("interval_First_eNB_ID", "enb_id"),
    ("interval_First_Cell_ID", "cell_id"),
    ("interval_First_TAC", "tac"),
    ("interval_First_eNB_VLAN_ID", "vlan_id"),
    ("interval_First_eNB_PLMN", "plmn"),
    ("MME_ID", "mme_id"), ("SGW_ID", "sgw_id"), ("PGW_ID", "pgw_id"),
    ("ims_PGW_ID", "ims_pgw_id"),
)


#: 전환 상대를 고를 때 쓰는 셀 목록. `place()` 가 `Generator` 를 모르므로
#: 호출부가 한 번 채워 준다 — 없으면 전환이 섹터 안에서만 일어난다.
CELL_POOL: list = []


def _neighbour_of(cell, rng: random.Random):
    """전환 상대 — **다른 eNB**.

    정답지에서 셀이 바뀐 20호 중 **19호가 eNB 간**이고 **TAC 변경은 0**
    이다. 같은 TAC 안에서 eNB 가 바뀌는 모양이고, 그래서 TAU 가 뒤따르지
    않는다.

    그런데 `data/cells.json` 은 **TAC 하나에 eNB 하나**다(116 : 116 ·
    TAC 당 셀 3개 = 한 eNB 의 3섹터). 1세대 산출물이라 실제 망 구조와
    다르다 — 같은 TAC 에서 다른 eNB 를 고를 수가 없다.

    그래서 **eNB 는 아무 데서나 고르고 `TAC` 는 출발 셀 것을 쓴다**
    (`place()` 가 그렇게 적는다). 전환 사실과 "TAC 는 그대로" 를 둘 다
    지키는 길이고, 셀 마스터가 실제 구조로 바뀌면 이 함수만 고치면 된다.
    """
    if not CELL_POOL:
        return cell
    for _ in range(8):
        c = CELL_POOL[rng.randrange(len(CELL_POOL))]
        if getattr(c, "enb_id", None) != getattr(cell, "enb_id", None):
            return c
    return cell


def place(rec: dict, cell, user: dict, rng: random.Random,
          came_from=None) -> dict:
    """셀·가입자를 끼운다.

    **직전 호의 위치(`old_call_Last_*`)는 같은 셀로 둔다.** 핸드오버가
    아니면 같은 자리에서 다시 시도한 것이고, 실측에서도 그 필드가
    `First_*` 와 같은 eNB 인 경우가 대부분이다. 이웃 셀로 보내려면
    호출부가 덮어쓴다.
    """
    out = dict(rec)
    # **전환이 있던 호는 `Last_*` 가 다른 셀이다.** 패턴이 `_moved` 로
    # 그 사실을 들고 온다 — First/Last 를 같은 셀로 덮으면 "한 호 안에서
    # 셀이 바뀌었다" 가 통째로 사라지고 `PathSwitch_count` 만 남는다.
    # 떠난 자리를 가리키는 축(`j-sc-15` 의 셀 쌍)이 거기서 끊긴다.
    moved = str(out.pop("_moved", "0")) == "1"
    dst = _neighbour_of(cell, rng) if moved else cell
    for col, attr in CELL_MAP:
        if col not in out:
            continue
        # **`TAC` 는 출발 셀 것이다.** 정답지에 TAC 변경이 0 이다 —
        # 전환이 같은 TA 안에서 일어나므로 TAU 가 뒤따르지 않는다.
        who = (dst if (moved and col.startswith("Last_") and attr != "tac")
               else cell)
        v = getattr(who, attr, "")
        out[col] = enb_number(v) if attr == "enb_id" else str(v or "")
    # 직전 호의 자리. ``came_from`` 이 오면 **거기서 넘어온 것**이다
    # (핸드오버) — 그러면 `old_call_type` 이 없어도 적는다. 어디서
    # 왔는지가 없으면 "넘어왔다"는 말만 있고 셀 쌍 분석이 못 선다
    # (`_baseline_old_call` 머리말 ①).
    prev = came_from or cell
    for col, attr in (("old_call_Last_eNB_ID", "enb_id"),
                      ("old_call_Last_Cell_ID", "cell_id"),
                      ("old_call_Last_TAC", "tac"),
                      ("old_call_Last_eNB_PLMN", "plmn")):
        # 직전 호 기록이 **있던 자리만** 채운다 — 없던 호에 만들어 넣으면
        # `OLD_FAIL_CNT` 의 분모가 장애와 무관하게 늘어난다.
        if came_from is not None or (out.get(col) == ""
                                     and out.get("old_call_type", "")):
            v = getattr(prev, attr, "")
            out[col] = enb_number(v) if attr == "enb_id" else str(v or "")
    for col, v in user.items():
        if col in out:
            out[col] = str(v or "")
    return out


# ── 데이터셋 ───────────────────────────────────────────────────────────────


def build(gen, bank: Bank, *, keys=None, n: int = 1000, ts=None,
          rng: random.Random | None = None,
          derive: "Derive | None" = None, call_type: int | None = None
          ) -> list[dict]:
    """패턴에서 레코드 ``n`` 건.

    ``gen`` 은 `Generator` — 셀·가입자만 빌린다. **레코드를 만들지
    않는다**(패턴이 그 일을 한다).

    ``derive`` 와 ``call_type`` 을 함께 주면 **정답지에 없는 절차**를
    있는 패턴에서 만든다(`config/callflow-derive.yaml`).
    """
    from datetime import datetime

    rng = rng or random.Random(20261028)
    ts = ts or datetime(2026, 10, 28, 9, 0)
    slot = ts.timestamp()
    if not CELL_POOL:
        CELL_POOL.extend(gen.cells)
    derived = derive is not None and call_type is not None \
        and call_type in derive.rules
    if derived:
        keys = derive.keys_for(bank, call_type)
    keys = keys or bank.keys
    out = []
    for _ in range(n):
        key, pat = bank.draw(keys, rng)
        if derived:
            pat = derive.picks(bank.by_key[key], call_type, rng)
        cell = gen.cells[rng.randrange(len(gen.cells))]
        sub = gen.subs[rng.randrange(len(gen.subs))]
        user = _user_fields(gen, sub)
        rec = stamp(pat, slot + rng.random() * 60)
        if derived:
            rec = derive.apply(rec, call_type, rng)
        src = None
        if derived and derive.wants_handover(call_type):
            # **전환 후 값이 레코드에 남는다** — 떠난 자리는
            # `old_call_Last_*` 가 가리킨다(`_apply_handover` 머리말).
            src = cell
            cell = _neighbour(gen, cell, rng)
        out.append(place(rec, cell, user, rng, came_from=src))
    return out


def _neighbour(gen, cell, rng: random.Random):
    """같은 TAC 안의 다른 eNB. 없으면 아무 셀."""
    pool = [c for c in gen.cells
            if getattr(c, "tac", None) == getattr(cell, "tac", None)
            and getattr(c, "enb_id", None) != getattr(cell, "enb_id", None)]
    return rng.choice(pool) if pool else gen.cells[rng.randrange(len(gen.cells))]


def _user_fields(gen, sub) -> dict:
    """가입자 식별자 — `Generator` 가 만든 합성값을 쓴다.

    **실측 암호문을 쓰지 않는다**(불변규칙 14 · PoC 는 체계만 맞춘 합성
    평문이다 — 2026-10-13 결정). `generator.user_values` 가 정본이고
    `USER_FIELDS` 와 짝이므로 둘을 그대로 묶는다.
    """
    from .generator import USER_FIELDS, user_values

    return dict(zip(USER_FIELDS, (str(v) for v in user_values(sub))))


#: 암호문 eNB ID → 평문 숫자. **규격이 `NUMBER(10)` 이다**(불변규칙 14 —
#: *"셀·노드·장비 키는 평문이다"*). 구 `data/cells.json` 은 1세대 산출물이라
#: `enb_id` 가 base64 암호문인데, 운영은 `CL_CELL_MAP` 에서 평문을 읽는다.
#: **같은 암호문은 늘 같은 숫자**여야 셀 축 집계가 갈리지 않는다.
_ENB_NUM: dict[str, str] = {}


def enb_number(raw) -> str:
    """eNB ID 를 평문 숫자로. 이미 숫자면 그대로."""
    v = str(raw or "")
    if v.isdigit():
        return v
    if v not in _ENB_NUM:
        import hashlib
        h = hashlib.sha1(v.encode()).hexdigest()
        _ENB_NUM[v] = str(100000 + int(h[:8], 16) % 900000)
    return _ENB_NUM[v]


def write(recs: list[dict], path: Path) -> int:
    """Parquet 으로 — **원시와 같은 모양이어야 한다.**

    두 가지를 맞춘다.

    * **상용 컬럼명**(`CALL_TYPE`·`FIRST_ENB_ID`). 자체 필드명으로 쓰면
      `graphio.cell_agg` 가 **한 컬럼도 못 읽는다** — 만들어도 파이프라인에
      들어가지 못한다.
    * **전부 문자열**. `cell_agg` 머리말이 적듯 Parquet 컬럼이 전량 문자열
      이라 `= '1'`·`<> ''`·센티널 `'0'` 비교가 그대로 선다. 숫자로 접으면
      `'021'` 0채움과 플래그 비교가 어긋난다.

    분 귀속 컬럼(`EVENT_TIME`)도 함께 적는다 — 원시 Parquet 의 첫 컬럼이고
    집계가 그것으로 분을 가른다(`store/tables.py:XDR_TIME`).
    """
    import polars as pl

    from .pemdb1 import CPLANE_FIELDS, CPLANE_SLOT

    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, list[str]] = {}
    # 분 귀속 — `call_start_time`(epoch 소수)에서 분을 만든다.
    from datetime import datetime, timezone
    slots = []
    for r in recs:
        try:
            t = float(str(r.get("call_start_time") or 0) or 0)
        except ValueError:
            t = 0.0
        dt = datetime.fromtimestamp(t, tz=timezone.utc).replace(
            second=0, microsecond=0, tzinfo=None) if t > 0 else None
        slots.append(dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "")
    data[CPLANE_SLOT] = slots
    for dst, src, *_ in CPLANE_FIELDS:
        data[dst] = [str(r.get(src, "") or "") for r in recs]
    pl.DataFrame(data).write_parquet(path, compression="zstd")
    return len(recs)


# ── 파생 — 정답지에 없는 call_type ─────────────────────────────────────────


DERIVE_SPEC = Path(__file__).resolve().parents[2] / "config" / "callflow-derive.yaml"


class Derive:
    """정답지에 **없는** call_type 을 있는 패턴에서 만든다.

    실측 표본 204호에 여섯 종만 있다(1·3·4·5·6). 나머지를 **지어내지
    않고** 파생한다 — 각 call_type 이 무엇으로 갈리는지 정답지가 100%
    결정적으로 말해 주기 때문이다. 규칙은 `config/callflow-derive.yaml`
    이고 **그쪽이 정본이다.**
    """

    def __init__(self, path: Path | str | None = None):
        import yaml

        p = Path(path) if path else DERIVE_SPEC
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        self.rules = {int(k): v for k, v in (raw.get("derive") or {}).items()}

    def sources(self, call_type: int) -> list[int]:
        r = self.rules.get(call_type) or {}
        if "from_any" in r:
            return [int(x) for x in r["from_any"]]
        return [int(r["from"])] if "from" in r else []

    def keys_for(self, bank: Bank, call_type: int) -> list[tuple]:
        """그 파생이 쓸 원본 키들. `require` 는 **패턴 내용**으로 거른다."""
        r = self.rules.get(call_type) or {}
        out = []
        for src in self.sources(call_type):
            for k in bank.match(call_type=src):
                if not r.get("require"):
                    out.append(k)
                    continue
                # 조건에 맞는 패턴이 **하나라도** 있는 키만 남긴다.
                if any(all(str(p.get(c, "")) == str(v)
                           for c, v in r["require"].items())
                       for p in bank.by_key[k]):
                    out.append(k)
        return out

    def apply(self, rec: dict, call_type: int, rng: random.Random) -> dict:
        """파생 규칙을 레코드에 적는다. **`call_type` 도 여기서 바꾼다.**"""
        r = self.rules.get(call_type)
        if not r:
            return rec
        out = dict(rec)
        out["call_type"] = str(call_type)
        for k, v in (r.get("set") or {}).items():
            out[k] = str(v)
        for k, dist in (r.get("draw") or {}).items():
            ks = list(dist)
            out[k] = str(rng.choices(ks, weights=[dist[x] for x in ks], k=1)[0])
        return out

    def wants_handover(self, call_type: int) -> bool:
        return bool((self.rules.get(call_type) or {}).get("handover"))

    def picks(self, pats: list[dict], call_type: int,
              rng: random.Random) -> dict:
        """`require` 를 만족하는 패턴 중 하나."""
        r = self.rules.get(call_type) or {}
        req = r.get("require")
        if req:
            ok = [p for p in pats
                  if all(str(p.get(c, "")) == str(v) for c, v in req.items())]
            if ok:
                return rng.choice(ok)
        return rng.choice(pats)
