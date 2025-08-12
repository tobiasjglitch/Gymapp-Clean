# app.py — Gymapp (Streamlit + Supabase)
# UI på svenska, mobilvänligt. Ingen PIN.
#
# Databas:
#   exercises(id, name, cue, icon_path)
#   program_weeks(week, day, exercise_id, sets, rep_min, rep_max)   # dag-kolumnen heter "day"
#   workouts(id, date, day_label)
#   sets(workout_id, exercise_id, set_no, reps, weight_kg, pr_flag)
#
# Viktigt:
# - Vi visar "Pass 1–4" i UI, men använder fortfarande "Upper A", "Lower A", "Upper B", "Lower B" i DB.
# - "Spara hela passet" gör DB-skrivning (inga tomma workouts).
# - Double progression inkl. -5 % backoff (två pass i rad under rep_min).
# - Deload vecka 12: vikt sänks (0.6x) och setvolym sänks i seed.
# - Vikt anges EN gång per övning. Reps per set.
#
# OBS: Om "Program"-fliken visar fel vecka, justera i UI så att den läser st.session_state["active_week"].

import os
from datetime import date
from typing import List, Tuple, Optional, Dict

import streamlit as st
from supabase import create_client, Client
import pandas as pd

# =========================
# ---- Grundinställningar
# =========================
st.set_page_config(page_title="Gymapp", page_icon="💪", layout="centered")

# Mobilvänlig stil
st.markdown("""
<style>
:root { --pill-bg:#f1f5f9; --pill-fg:#0f172a; }
.badge{display:inline-block;padding:.15rem .5rem;border-radius:9999px;background:var(--pill-bg);color:var(--pill-fg);font-weight:600}
.pill{display:inline-block;padding:.15rem .5rem;border-radius:9999px;background:#e2e8f0;color:#111827;font-weight:600}
.pill-live{background:#dcfce7;color:#14532d}
.stButton>button, [data-testid="stFormSubmitButton"] button{min-height:56px}
[data-testid="stHeader"]{position:sticky;top:0;background:var(--background-color);z-index:1000}
</style>
""", unsafe_allow_html=True)

# =========================
# ---- Dag-mappning (UI ↔ DB)
# =========================
# DB har: "Upper A", "Lower A", "Upper B", "Lower B"
DAY_CANON = ["Upper A", "Lower A", "Upper B", "Lower B"]      # används mot DB
DAY_UI    = ["Pass 1", "Pass 2", "Pass 3", "Pass 4"]          # visas i UI

ui_to_canon = dict(zip(DAY_UI, DAY_CANON))
canon_to_ui = dict(zip(DAY_CANON, DAY_UI))

# =========================
# ---- Supabase-klient
# =========================
def _read_supabase_creds() -> Tuple[Optional[str], Optional[str]]:
    # 1) streamlit secrets
    url = st.secrets.get("supabase", {}).get("url") if "supabase" in st.secrets else st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("supabase", {}).get("anon_key") if "supabase" in st.secrets else st.secrets.get("SUPABASE_KEY")
    if url and key:
        return url, key
    # 2) env
    url = os.environ.get("SUPABASE_URL") or url
    key = os.environ.get("SUPABASE_KEY") or key
    if url and key:
        return url, key
    # 3) minimal TOML-läsare lokalt
    def _load_toml_min(path: str):
        data, current = {}, None
        if not os.path.exists(path): return {}
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"): continue
                if line.startswith("[") and line.endswith("]"):
                    current = line.strip("[]")
                    data[current] = {}
                elif "=" in line:
                    k,v = line.split("=",1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if current: data[current][k]=v
                    else: data[k]=v
        return data
    for p in ("./streamlit_config/secrets.toml",
              os.path.expanduser("~/Documents/Gymapp/streamlit_config/secrets.toml")):
        try:
            sec = _load_toml_min(p)
            sec = sec.get("supabase", sec)
            url = sec.get("url") or sec.get("SUPABASE_URL")
            key = sec.get("anon_key") or sec.get("SUPABASE_KEY")
            if url and key: return url, key
        except Exception:
            pass
    return None, None

def get_supabase_client() -> Client:
    url, key = _read_supabase_creds()
    if not url or not key:
        st.error("Hittar inte Supabase-nycklar. Lägg dem under [supabase] url/anon_key eller SUPABASE_URL/SUPABASE_KEY.")
        st.stop()
    return create_client(url, key)

sb: Client = get_supabase_client()

# =========================
# ---- Konstanter & Helpers
# =========================
DEFAULT_DELOAD_FACTOR = 0.6

def is_lower_body(name: str) -> bool:
    low = name.lower()
    keys = ["böj", "squat", "mark", "lunges", "vadpress", "hip", "thrust", "pull-through", "calf"]
    return any(k in low for k in keys)

def phase_for_week(idx: int) -> str:
    v = idx + 1
    if 1 <= v <= 8: return "Hypertrofi"
    if 9 <= v <= 11: return "Styrka"
    return "Deload"

def double_progression_bump(ex_name: str) -> float:
    # +2.5 kg press/rygg, +5 kg ben/hip (heuristiskt via namn)
    return 5.0 if is_lower_body(ex_name) else 2.5

# =========================
# ---- DB helpers
# =========================
def fetch_exercise(ex_id: str) -> Optional[Dict]:
    res = sb.from_("exercises").select("*").eq("id", ex_id).limit(1).execute().data
    return res[0] if res else None

def fetch_program_for_day(week_idx: int, canon_day: str) -> List[Dict]:
    # week_idx 0..11 -> DB week 1..12
    w = week_idx + 1
    rows = (
        sb.from_("program_weeks")
        .select("*,exercises(*)")
        .eq("week", w)
        .eq("day", canon_day)
        .order("exercise_id", desc=False)
        .execute()
        .data or []
    )
    return rows

def fetch_sets_for_workout(workout_id: str) -> List[Dict]:
    return (
        sb.from_("sets")
        .select("*")
        .eq("workout_id", workout_id)
        .order("exercise_id", desc=False)
        .order("set_no", desc=False)
        .execute()
        .data or []
    )

def personal_bests_map() -> Dict[Tuple[str,float], int]:
    # (exercise_id, weight) -> max reps
    rows = (
        sb.from_("sets")
        .select("exercise_id,weight_kg,reps")
        .execute()
        .data or []
    )
    best: Dict[Tuple[str,float], int] = {}
    for r in rows:
        key = (r["exercise_id"], float(r["weight_kg"]))
        best[key] = max(best.get(key, 0), int(r["reps"]))
    return best

# =========================
# ---- Progressionslogik
# =========================
def propose_weight(ex_name: str, rep_min: int, rep_max: int, week_idx: int, history: List[Dict]) -> float:
    """
    Double progression:
      - När alla set når rep_max → +2.5 kg (press/rygg) / +5 kg (ben/hip)
      - Två pass i rad under rep_min → −5% vikt
      - Vecka 12 (Deload) → multiplicera med 0.6
    """
    # Hitta senaste vikt + reps
    last_weight = None
    last_all_sets = []
    for h in history:
        if h["exercise"] == ex_name:
            last_weight = float(h["weight"])
            last_all_sets = list(map(int, h["reps"]))
            break

    # Ingen historik – föreslå "startvikt" via cue (om siffra finns) annars 0
    if last_weight is None:
        ex = (
            sb.from_("exercises")
            .select("cue")
            .eq("name", ex_name)
            .limit(1)
            .execute()
            .data
        )
        start_weight = 0.0
        if ex and ex[0].get("cue"):
            import re
            m = re.search(r"(\d+(\.\d+)?)", ex[0]["cue"])
            if m: start_weight = float(m.group(1))
        last_weight = start_weight

    bump = double_progression_bump(ex_name)

    # två pass i rad under rep_min?
    under_min_streak = 0
    for h in history:
        if h["exercise"] == ex_name:
            if all(int(r) < rep_min for r in h["reps"]):
                under_min_streak += 1
            else:
                break
    if under_min_streak >= 2:
        last_weight = round(last_weight * 0.95, 1)

    # alla set nådde rep_max i senaste passet?
    if last_all_sets and all(r >= rep_max for r in last_all_sets):
        last_weight = round(last_weight + bump, 1)

    # deload v12
    if (week_idx + 1) == 12:
        last_weight = round(last_weight * DEFAULT_DELOAD_FACTOR, 1)

    return max(last_weight, 0.0)

# =========================
# ---- Historik (för förslag)
# =========================
def compact_history_for_day(canon_day: str) -> List[Dict]:
    """
    Returnerar förenklad historik för kanoniskt dag-namn (Upper/Lower A/B):
    [{exercise, weight, reps:[..]} ...]
    """
    rows = (
        sb.from_("workouts")
        .select("id, date, day_label, sets(*)")
        .eq("day_label", canon_day)
        .order("date", desc=True)
        .limit(10)
        .execute()
        .data or []
    )
    out = []
    for w in rows:
        sets = w.get("sets") or []
        per_ex: Dict[str, Dict] = {}
        for s in sets:
            eid = s["exercise_id"]
            per_ex.setdefault(eid, {"weight": float(s["weight_kg"]), "reps": []})
            per_ex[eid]["reps"].append(int(s["reps"]))
        for eid, rec in per_ex.items():
            ex = fetch_exercise(eid) or {}
            out.append({
                "exercise_id": eid,
                "exercise": ex.get("name", eid[:8]),
                "weight": rec["weight"],
                "reps": rec["reps"],
            })
    return out

# =========================
# ---- Header ----------------
# =========================
today = date.today()

# Global veckoväljare (styr "Idag" + deload)
if "active_week" not in st.session_state:
    st.session_state["active_week"] = 1  # start alltid på vecka 1
wk = st.number_input("Vecka (1–12)", min_value=1, max_value=12, step=1,
                     value=int(st.session_state["active_week"]), key="active_week_input")
st.session_state["active_week"] = int(wk)
widx = st.session_state["active_week"] - 1  # 0-index internt

st.markdown(
    f"### 💪 Gymapp  &nbsp;&nbsp; **{today.strftime('%Y-%m-%d')}** &nbsp;&nbsp; "
    f"**Vecka {widx+1}** <span class='badge'>{phase_for_week(widx)}</span>  "
    f"&nbsp;&nbsp; <span class='pill pill-live'>🟢 LIVE</span>",
    unsafe_allow_html=True
)

tabs = st.tabs(["Idag", "Program", "Historik", "Export"])

# =========================
# ---- IDAG ----------------
# =========================
with tabs[0]:
    st.subheader("Idag")

    # Passväljare (UI) -> mappa till kanoniskt dag-namn för DB
    if "active_day_ui" not in st.session_state:
        st.session_state["active_day_ui"] = DAY_UI[0]
    day_ui = st.selectbox("Dagens pass", DAY_UI, index=DAY_UI.index(st.session_state["active_day_ui"]))
    st.session_state["active_day_ui"] = day_ui
    day_canon = ui_to_canon[day_ui]

    plan = fetch_program_for_day(widx, day_canon)
    if not plan:
        st.info("Inget program hittat för den här veckan/dagen. Gå till fliken **Program** och klicka ”Initiera programdata”.")
    else:
        with st.form("today_form"):
            collected: List[Tuple[str, str, int, List[int], float]] = []  # (exercise_id, name, sets, reps[], weight)

            hist = compact_history_for_day(day_canon)
            bests = personal_bests_map()

            for i, row in enumerate(plan):
                ex = row["exercises"] or {}
                name = ex.get("name", f"Övning {row['exercise_id'][:8]}")
                sets_n = int(row["sets"])
                rep_min = int(row["rep_min"])
                rep_max = int(row["rep_max"])

                suggested = propose_weight(name, rep_min, rep_max, widx, hist)

                st.markdown(f"**{name}**  &nbsp; <span class='badge'>{rep_min}–{rep_max} reps × {sets_n} set</span>", unsafe_allow_html=True)
                c1, c2 = st.columns([1,1])
                with c1:
                    weight = st.number_input("Vikt (kg)", min_value=0.0, max_value=999.0, step=0.5, value=float(suggested), key=f"w_{i}")
                with c2:
                    st.caption(f"Förslag: {suggested} kg")

                reps_val: List[int] = []
                for s in range(1, sets_n + 1):
                    reps = st.number_input(f"Set {s} reps", min_value=0, max_value=50, step=1, value=rep_min, key=f"r_{i}_{s}")
                    reps_val.append(int(reps))

                collected.append((row["exercise_id"], name, sets_n, reps_val, float(weight)))

            submitted = st.form_submit_button("💾 Spara hela passet", use_container_width=True)
            if submitted:
                with st.spinner("Sparar passet..."):
                    try:
                        # Spara workout med KANONISKT dag-namn (för kompabilitet)
                        ins = sb.from_("workouts").insert({"date": today.isoformat(), "day_label": day_canon}).execute().data
                        workout_id = ins[0]["id"]

                        # Spara set + PR-flagga
                        current_bests = personal_bests_map()
                        to_insert = []
                        pr_flags = []
                        for ex_id, name, sets_n, reps_val, weight in collected:
                            prev_reps = current_bests.get((ex_id, float(weight)), 0)
                            pr_local = any(rv > prev_reps for rv in reps_val)
                            for s_idx, rv in enumerate(reps_val, start=1):
                                to_insert.append({
                                    "workout_id": workout_id,
                                    "exercise_id": ex_id,
                                    "set_no": s_idx,
                                    "reps": rv,
                                    "weight_kg": weight,
                                    "pr_flag": pr_local,
                                })
                            if pr_local:
                                pr_flags.append(name)
                        if to_insert:
                            sb.from_("sets").insert(to_insert).execute()

                        if pr_flags:
                            st.success("Pass sparat! 🎉 PB på: " + ", ".join(pr_flags))
                        else:
                            st.success("Pass sparat!")
                        st.balloons()
                    except Exception as e:
                        st.error(f"Kunde inte spara: {e}")

# =========================
# ---- PROGRAM ----------------
# =========================
def seed_program() -> int:
    """
    Skapar 12 veckors program för bröstfokus, utan bänkpress, med axelpress.
      v1–4: Hypertrofi (baseline)
      v5–8: Hypertrofi (accessoar-variation så du inte tröttnar)
      v9–11: Styrka (3–5 bas, 6–8 assistans, -1 set på assistans)
      v12: Deload (~60% setvolym; vikt-sänkning sköts även av appens logik)
    Huvudlyft hålls konstanta. Accessoarer roteras.
    """

    # --- Hämta övningar
    ex_rows = sb.from_("exercises").select("id,name").execute().data or []
    name_to_id = {r["name"]: r["id"] for r in ex_rows}

    def _resolve(name_to_id: Dict[str,str], *aliases: str) -> Optional[str]:
        # exakt träff
        for a in aliases:
            if a in name_to_id:
                return name_to_id[a]
        # fuzzy: innehåller
        lowmap = {k.lower(): v for k,v in name_to_id.items()}
        for a in aliases:
            a_low = a.lower()
            for k,v in lowmap.items():
                if a_low in k:
                    return v
        return None

    # --- Basmall (v1–4): kanoniska dag-namn
    base_template: Dict[str, List[Tuple[str, bool, int, Tuple[str,...]]]] = {
        # Pass 1 — Upper A (Bröst/triceps, hypertrofi)
        "Upper A": [
            ("Lutande hantelpress", True, 4, ("Lutande hantelpress","Lutande press")),
            ("Kabel-flyes (hög→låg)", False, 3, ("Kabel-flyes (hög→låg)","Kabel-flyes hög","Kabel flyes hög")),
            ("Enarms kabelpress", False, 3, ("Enarms kabelpress","Kabelpress")),
            ("Enarms hantelrodd", False, 3, ("Enarms hantelrodd","Hantelrodd")),
            ("Sidolyft hantlar", False, 3, ("Sidolyft hantlar","Sidolyft")),
            ("Triceps pushdown", False, 3, ("Triceps pushdown","Pushdown")),
        ],
        # Pass 2 — Lower A
        "Lower A": [
            ("Knäböj", True, 4, ("Knäböj","Böj","Squat")),
            ("Raka marklyft (RDL)", True, 4, ("Raka marklyft (RDL)","RDL","Raka marklyft")),
            ("Bulgarian split squat", False, 3, ("Bulgarian split squat","Bulgarian")),
            ("Kabel pull-through", False, 3, ("Kabel pull-through","Pull-through")),
            ("Vadpress", False, 3, ("Vadpress","Calf raise")),
            ("Kabel-crunch", False, 3, ("Kabel-crunch","Cable crunch")),
        ],
        # Pass 3 — Upper B (Bröst tungt + axlar/rygg/biceps)
        "Upper B": [
            ("Hantelpress plan bänk", True, 4, ("Hantelpress plan bänk","Hantelpress")),
            ("Kabel-flyes (låg→hög)", False, 3, ("Kabel-flyes (låg→hög)","Kabel-flyes låg","Kabel flyes låg")),
            ("Lutande kabelpress", False, 3, ("Lutande kabelpress","Kabelpress")),
            ("Sittande kabelrodd", False, 3, ("Sittande kabelrodd","Kabelrodd")),
            ("Face pull", False, 3, ("Face pull","Facepull")),
            ("Axelpress hantlar", False, 3, ("Axelpress hantlar","Axelpress")),
            ("Bicepscurl hantlar", False, 3, ("Bicepscurl hantlar","Bicepscurl")),
        ],
        # Pass 4 — Lower B
        "Lower B": [
            ("Marklyft", True, 3, ("Marklyft","Mark")),
            ("Frontböj", True, 3, ("Frontböj","Front squat","Goblet squat","Goblet")),
            ("Hip thrust", True, 4, ("Hip thrust","Hipthrust")),
            ("Bakåtlunges", False, 3, ("Bakåtlunges","Lunges bak")),
            ("Vadpress", False, 3, ("Vadpress","Calf raise")),
            ("Kabel woodchop", False, 3, ("Kabel woodchop","Woodchop")),
        ],
    }

    # --- Variation (v5–8): byter några accessoarer
    var_template: Dict[str, List[Tuple[str, bool, int, Tuple[str,...]]]] = {
        "Upper A": [
            ("Lutande hantelpress", True, 4, ("Lutande hantelpress","Lutande press")),
            ("Kabel-flyes (låg→hög)", False, 3, ("Kabel-flyes (låg→hög)","Kabel-flyes låg","Kabel flyes låg")),  # swap vinkel
            ("Lutande kabelpress", False, 3, ("Lutande kabelpress","Kabelpress")),                               # swap mot enarms kabelpress
            ("Sittande kabelrodd", False, 3, ("Sittande kabelrodd","Kabelrodd")),                                 # swap rodd
            ("Sidolyft hantlar", False, 3, ("Sidolyft hantlar","Sidolyft")),
            ("Triceps pushdown", False, 3, ("Triceps pushdown","Pushdown")),
        ],
        "Lower A": [
            ("Knäböj", True, 4, ("Knäböj","Böj","Squat")),
            ("Raka marklyft (RDL)", True, 4, ("Raka marklyft (RDL)","RDL","Raka marklyft")),
            ("Bakåtlunges", False, 3, ("Bakåtlunges","Lunges bak")),   # swap mot Bulgarian
            ("Kabel pull-through", False, 3, ("Kabel pull-through","Pull-through")),
            ("Vadpress", False, 3, ("Vadpress","Calf raise")),
            ("Kabel-crunch", False, 3, ("Kabel-crunch","Cable crunch")),
        ],
        "Upper B": [
            ("Hantelpress plan bänk", True, 4, ("Hantelpress plan bänk","Hantelpress")),
            ("Kabel-flyes (hög→låg)", False, 3, ("Kabel-flyes (hög→låg)","Kabel-flyes hög","Kabel flyes hög")),  # swap vinkel
            ("Enarms kabelpress", False, 3, ("Enarms kabelpress","Kabelpress")),                                  # swap mot lutande kabelpress
            ("Sittande kabelrodd", False, 3, ("Sittande kabelrodd","Kabelrodd")),
            ("Face pull", False, 3, ("Face pull","Facepull")),
            ("Axelpress hantlar", False, 3, ("Axelpress hantlar","Axelpress")),
            ("Bicepscurl hantlar", False, 3, ("Bicepscurl hantlar","Bicepscurl")),
        ],
        "Lower B": [
            ("Marklyft", True, 3, ("Marklyft","Mark")),
            ("Goblet squat", True, 3, ("Goblet squat","Goblet","Frontböj","Front squat")),   # swap mot Frontböj om finns
            ("Hip thrust", True, 4, ("Hip thrust","Hipthrust")),
            ("Bulgarian split squat", False, 3, ("Bulgarian split squat","Bulgarian")),      # swap mot bakåtlunges
            ("Vadpress", False, 3, ("Vadpress","Calf raise")),
            ("Kabel woodchop", False, 3, ("Kabel woodchop","Woodchop")),
        ],
    }

    def _block_for_week(week: int) -> str:
        if week <= 8: return "Hypertrofi"
        if 9 <= week <= 11: return "Styrka"
        return "Deload"

    rows = []
    for week in range(1, 13):
        block = _block_for_week(week)
        # välj mall
        tpl = base_template if week <= 4 else (var_template if week <= 8 else var_template)

        for canon_day in DAY_CANON:
            for name, is_base, sets_n, aliases in tpl[canon_day]:
                ex_id = _resolve(name_to_id, *aliases) or name_to_id.get(name)
                if not ex_id:
                    # hoppa över om övningen inte finns i tabellen
                    continue

                # reps per block
                if block == "Hypertrofi" or block == "Deload":
                    rep_min, rep_max = (6,10) if is_base else (8,12)
                else:  # Styrka
                    rep_min, rep_max = (3,5) if is_base else (6,8)

                # set-justering per block
                sets_out = sets_n
                if block == "Styrka" and not is_base:
                    sets_out = max(2, sets_n - 1)  # lite lägre assistansvolym
                if block == "Deload":
                    # Sänk setvolym ~40% (min 2 set)
                    calc = int(round(sets_n * 0.6))
                    sets_out = max(2, calc)

                rows.append({
                    "week": week,
                    "day": canon_day,     # Viktigt: behåll kanoniskt dag-namn i DB
                    "exercise_id": ex_id,
                    "sets": int(sets_out),
                    "rep_min": int(rep_min),
                    "rep_max": int(rep_max),
                })

    # Rensa och skriv in
    sb.table("program_weeks").delete().neq("week", -1).execute()
    if rows:
        BATCH = 200
        for i in range(0, len(rows), BATCH):
            sb.table("program_weeks").insert(rows[i:i+BATCH]).execute()
    return len(rows)

with tabs[1]:
    st.subheader("Program")
    st.caption("v1–4 Hypertrofi • v5–8 Hypertrofi (variation) • v9–11 Styrka • v12 Deload")

    # Synka med toppens veckoval
    sel_week = st.number_input("Vecka (1–12)", min_value=1, max_value=12, step=1,
                               value=st.session_state["active_week"], key="program_week")
    st.session_state["active_week"] = int(sel_week)

    if st.button("⚙️ Initiera programdata (12 veckor)", use_container_width=True):
        with st.spinner("Initierar programdata..."):
            try:
                n = seed_program()
                st.success(f"Programdata skapad/uppdaterad ({n} rader).")
            except Exception as e:
                st.error(f"Kunde inte initiera: {e}")

    # Hämta veckans rader
    plan_rows = (
        sb.from_("program_weeks")
        .select("*,exercises(name)")
        .eq("week", int(sel_week))
        .order("day", desc=False)
        .order("exercise_id", desc=False)
        .execute()
        .data or []
    )

    if not plan_rows:
        st.info("Inget program hittat. Klicka ”Initiera programdata”.")
    else:
        # Visa dag för dag med UI-namnet "Pass X"
        for canon_day in DAY_CANON:
            day_rows = [r for r in plan_rows if r["day"] == canon_day]
            if not day_rows:
                continue
            st.markdown(f"### {canon_to_ui[canon_day]}  <span class='badge'>{canon_day}</span>", unsafe_allow_html=True)

            with st.form(f"program_form_{canon_day}"):
                rows_to_save = []
                valid_form = True
                for i, row in enumerate(day_rows):
                    ex = row["exercises"] or {}
                    name = ex.get("name", f"Övning {row['exercise_id'][:8]}")
                    c1,c2,c3,c4 = st.columns([2,1,1,1])
                    with c1: st.markdown(f"**{name}**")
                    with c2: sets_v = st.number_input("Set", 1, 8, int(row["sets"]), key=f"pg_sets_{canon_day}_{i}")
                    with c3: rmin_v = st.number_input("Rep min", 1, 30, int(row["rep_min"]), key=f"pg_min_{canon_day}_{i}")
                    with c4: rmax_v = st.number_input("Rep max", 1, 30, int(row["rep_max"]), key=f"pg_max_{canon_day}_{i}")

                    if rmax_v < rmin_v:
                        st.error(f"⚠️ Rep max för {name} måste vara ≥ Rep min.", icon="🚨")
                        valid_form = False

                    rows_to_save.append((row["exercise_id"], sets_v, rmin_v, rmax_v))

                saved = st.form_submit_button("💾 Spara ändringar för detta pass", use_container_width=True)
                if saved:
                    if not valid_form:
                        st.error("Korrigera fel innan du sparar.")
                    else:
                        with st.spinner("Sparar..."):
                            try:
                                for ex_id, sets_v, rmin_v, rmax_v in rows_to_save:
                                    sb.table("program_weeks").update({
                                        "sets": int(sets_v),
                                        "rep_min": int(rmin_v),
                                        "rep_max": int(rmax_v),
                                    }).match({
                                        "week": int(sel_week),
                                        "day": canon_day,
                                        "exercise_id": ex_id,
                                    }).execute()
                                st.success("Program uppdaterat.")
                            except Exception as e:
                                st.error(f"Kunde inte spara: {e}")

# =========================
# ---- HISTORIK ----------------
# =========================
with tabs[2]:
    st.subheader("Historik")
    # Filter visas som "Pass X" men matchar DB via kanoniskt namn
    filt_ui = st.selectbox("Filtrera på pass", ["Alla"] + DAY_UI, index=0)
    go = st.checkbox("Visa set per övning")

    if st.button("🔄 Uppdatera", use_container_width=True):
        st.experimental_rerun()

    if filt_ui == "Alla":
        with st.spinner("Hämtar data..."):
            data = (
                sb.from_("workouts")
                .select("*, sets(*), exercises:sets(exercises(*))")
                .order("date", desc=True)
                .limit(100)
                .execute()
                .data or []
            )
    else:
        canon = ui_to_canon[filt_ui]
        with st.spinner("Hämtar data..."):
            data = (
                sb.from_("workouts")
                .select("*, sets(*), exercises:sets(exercises(*))")
                .eq("day_label", canon)
                .order("date", desc=True)
                .limit(100)
                .execute()
                .data or []
            )

    if not data:
        st.info("Ingen historik ännu.")
    else:
        for w in data:
            # Visa både Pass X och kanoniskt namn
            ui_name = canon_to_ui.get(w['day_label'], w['day_label'])
            st.markdown(f"**{w['date']} — {ui_name}**  <span class='badge'>{w['day_label']}</span>", unsafe_allow_html=True)
            if go:
                for s in w.get("sets", []):
                    ex = s.get("exercises") or {}
                    name = ex.get("name", s['exercise_id'][:8])
                    pr = " 🏆" if s.get("pr_flag") else ""
                    st.write(f"- {name}: {s['weight_kg']} kg × {s['reps']} reps{pr}")

# =========================
# ---- EXPORT ----------------
# =========================
with tabs[3]:
    st.subheader("Export")
    st.caption("Ladda ner all träningsdata som CSV.")

    include_pr = st.checkbox("Ta med PR-flagga", value=True)
    if st.button("⤓ Skapa CSV", use_container_width=True):
        with st.spinner("Hämtar data..."):
            data = (
                sb.from_("sets")
                .select("*, workouts:workouts(*), exercises:exercises(*)")
                .order("workout_id", desc=False)
                .order("exercise_id", desc=False)
                .order("set_no", desc=False)
                .execute().data or []
            )
        if not data:
            st.warning("Inget att exportera ännu.")
        else:
            df = pd.DataFrame(data)
            df["date"] = df["workouts"].apply(lambda x: x.get("date") if isinstance(x, dict) else "")
            # Byt ut dag_label i exporten till "Pass X" för läsbarhet
            df["day_label"] = df["workouts"].apply(lambda x: canon_to_ui.get(x.get("day_label",""), x.get("day_label","")) if isinstance(x, dict) else "")
            df["exercise"] = df["exercises"].apply(lambda x: x.get("name") if isinstance(x, dict) else "")
            cols = ["date","day_label","exercise","set_no","weight_kg","reps"]
            if include_pr: cols.append("pr_flag")
            csv = df[cols].to_csv(index=False).encode("utf-8")
            st.download_button("⤓ Spara CSV", data=csv, file_name="gymapp_export.csv", mime="text/csv", use_container_width=True)
