import os
import re
from datetime import date
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    # Render va a mostrar este error en logs si faltan env vars
    raise RuntimeError("Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en Environment Variables")

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

MEAL_TYPES = {"DESAYUNO", "ALMUERZO", "CENA", "SNACK"}

ACTIVITY_MAP = {
    "1.2": ("Sedentario", 1.2),
    "1.375": ("Ligera", 1.375),
    "1.55": ("Moderada", 1.55),
    "1.725": ("Alta", 1.725),
    "1.9": ("Muy Alta (atletas)", 1.9),
}

GOALS = {"DEFICIT", "VOLUMEN"}  # según tu regla: deficit -400, volumen +300


# -----------------------------
# Telegram helpers
# -----------------------------
def tg_send(chat_id: int, text: str) -> None:
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


# -----------------------------
# DB helpers (Supabase tables)
# Asumimos que existen:
# - users: id (pk), telegram_chat_id (unique), display_name
# - user_state: user_id (pk), step, data(json)
# - user_profile: user_id (pk) ... macros
# -----------------------------
def get_or_create_user_id(chat_id: int, display_name: str) -> int:
    # Busca por telegram_chat_id; si no existe, crea.
    # Ajusta nombres de columna si tu tabla difiere.
    res = (
        sb.table("users")
        .select("id")
        .eq("telegram_chat_id", chat_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if rows:
        return int(rows[0]["id"])

    ins = (
        sb.table("users")
        .insert({"telegram_chat_id": chat_id, "display_name": display_name})
        .execute()
    )
    return int(ins.data[0]["id"])


def get_state(user_id: int) -> Dict[str, Any]:
    res = sb.table("user_state").select("step,data").eq("user_id", user_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        return {"step": None, "data": {}}
    return {"step": rows[0].get("step"), "data": rows[0].get("data") or {}}


def set_state(user_id: int, step: Optional[str], data: Dict[str, Any]) -> None:
    sb.table("user_state").upsert({"user_id": user_id, "step": step, "data": data}).execute()


def clear_state(user_id: int) -> None:
    # deja step None pero conserva data vacía
    sb.table("user_state").upsert({"user_id": user_id, "step": None, "data": {}}).execute()


def has_profile(user_id: int) -> bool:
    res = sb.table("user_profile").select("user_id").eq("user_id", user_id).limit(1).execute()
    return bool(res.data)


# -----------------------------
# Parsing de comida
# Ej: "pollo cocido 180g" / "arroz 200" / "pepino 100 g"
# -----------------------------
FOOD_RE = re.compile(r"^(?P<name>.+?)\s+(?P<grams>\d+(?:[.,]\d+)?)\s*g?$", re.IGNORECASE)

def parse_food_grams(text: str):
    m = FOOD_RE.match(text.strip())
    if not m:
        return None
    name = normalize_text(m.group("name"))
    grams_raw = m.group("grams").replace(",", ".")
    grams = float(grams_raw)
    return name, grams


# -----------------------------
# UX messages
# -----------------------------
def onboarding_intro() -> str:
    return (
        "Perfecto. Vamos a armar tus macros.\n"
        "Responde en este orden:\n"
        "1) Sexo: H o M\n"
        "2) Edad (años)\n"
        "3) Talla (cm)\n"
        "4) Peso (kg)\n"
        "5) Actividad: 1.2 / 1.375 / 1.55 / 1.725 / 1.9\n"
        "6) Objetivo: DEFICIT o VOLUMEN\n\n"
        "Tip: 1.9 es para atletas; la mayoría usa 1.55 o 1.725."
    )


def activity_menu() -> str:
    return (
        "Elige tu actividad escribiendo el número:\n"
        "1.2 Sedentario\n"
        "1.375 Ligera\n"
        "1.55 Moderada\n"
        "1.725 Alta\n"
        "1.9 Muy Alta (atletas)"
    )


def help_text() -> str:
    return (
        "Comandos:\n"
        "/start → iniciar\n"
        "RESUMEN → ver objetivo + progreso hoy\n"
        "DESAYUNO / ALMUERZO / CENA / SNACK → seleccionar comida\n"
        "Luego escribe: <alimento> <gramos>\n"
        "Ej: pollo cocido 180g\n"
        "Ej: arroz 200\n"
    )


# -----------------------------
# Core flow
# -----------------------------
def handle_start(chat_id: int, user_id: int) -> None:
    st = get_state(user_id)
    tg_send(chat_id, "¡Hola! 👋 Para comenzar, envíame tu código de acceso (ej: MVP-1001).")
    set_state(user_id, "WAIT_CODE", st["data"] or {})


def handle_code(chat_id: int, user_id: int, code: str) -> None:
    # RPC: activate_with_code(p_user_id, p_code)
    try:
        sb.rpc("activate_with_code", {"p_user_id": user_id, "p_code": code}).execute()
        tg_send(chat_id, "✅ Código válido. " + onboarding_intro())
        set_state(user_id, "ONB_SEX", {})
    except Exception:
        tg_send(chat_id, "❌ Código inválido. Intenta de nuevo (ej: MVP-1001).")


def finalize_onboarding(chat_id: int, user_id: int, data: Dict[str, Any]) -> None:
    # RPC: complete_onboarding(bigint,text,int,numeric,numeric,numeric,text)
    payload = {
        "p_user_id": user_id,
        "p_sex": data["sex"],
        "p_age": int(data["age"]),
        "p_height_cm": float(data["height_cm"]),
        "p_weight_kg": float(data["weight_kg"]),
        "p_activity_factor": float(data["activity_factor"]),
        "p_goal": data["goal"],
    }
    sb.rpc("complete_onboarding", payload).execute()

    # Mensaje final con objetivo (lo leemos de user_profile)
    prof = sb.table("user_profile").select("kcal_target,protein_g,carbs_g,fats_g").eq("user_id", user_id).limit(1).execute().data[0]
    tg_send(
        chat_id,
        "✅ Listo. Tus macros quedaron así:\n"
        f"- Calorías: {int(prof['kcal_target'])}\n"
        f"- Proteína: {int(prof['protein_g'])} g\n"
        f"- Carbos: {int(prof['carbs_g'])} g\n"
        f"- Grasas: {int(prof['fats_g'])} g\n\n"
        "Ahora puedes registrar comidas:\n"
        "1) Escribe: ALMUERZO (o DESAYUNO/CENA/SNACK)\n"
        "2) Luego: pollo cocido 180g\n\n"
        "Escribe RESUMEN cuando quieras ver tu avance."
    )
    set_state(user_id, None, {"current_meal": "ALMUERZO"})  # default cómodo


def handle_onboarding(chat_id: int, user_id: int, text: str) -> None:
    st = get_state(user_id)
    step = st["step"]
    data = st["data"] or {}

    t = normalize_text(text).upper()

    if step == "ONB_SEX":
        if t not in {"H", "M"}:
            tg_send(chat_id, "Escribe solo: H o M")
            return
        data["sex"] = t
        set_state(user_id, "ONB_AGE", data)
        tg_send(chat_id, "Edad (años):")
        return

    if step == "ONB_AGE":
        if not t.isdigit() or not (10 <= int(t) <= 90):
            tg_send(chat_id, "Edad inválida. Ej: 34")
            return
        data["age"] = int(t)
        set_state(user_id, "ONB_HEIGHT", data)
        tg_send(chat_id, "Talla en cm (ej: 163):")
        return

    if step == "ONB_HEIGHT":
        try:
            h = float(t.replace(",", "."))
            if not (120 <= h <= 230):
                raise ValueError()
        except Exception:
            tg_send(chat_id, "Talla inválida. Ej: 163")
            return
        data["height_cm"] = h
        set_state(user_id, "ONB_WEIGHT", data)
        tg_send(chat_id, "Peso en kg (ej: 67):")
        return

    if step == "ONB_WEIGHT":
        try:
            w = float(t.replace(",", "."))
            if not (35 <= w <= 250):
                raise ValueError()
        except Exception:
            tg_send(chat_id, "Peso inválido. Ej: 67")
            return
        data["weight_kg"] = w
        set_state(user_id, "ONB_ACTIVITY", data)
        tg_send(chat_id, activity_menu())
        return

    if step == "ONB_ACTIVITY":
        key = t.replace(" ", "")
        if key not in ACTIVITY_MAP:
            tg_send(chat_id, "Actividad inválida.\n" + activity_menu())
            return
        data["activity_factor"] = float(ACTIVITY_MAP[key][1])
        set_state(user_id, "ONB_GOAL", data)
        tg_send(chat_id, "Objetivo: DEFICIT o VOLUMEN")
        return

    if step == "ONB_GOAL":
        if t not in GOALS:
            tg_send(chat_id, "Objetivo inválido. Escribe: DEFICIT o VOLUMEN")
            return
        data["goal"] = t
        finalize_onboarding(chat_id, user_id, data)
        return


def handle_summary(chat_id: int, user_id: int) -> None:
    # objetivo
    prof_res = sb.table("user_profile").select("kcal_target,protein_g,carbs_g,fats_g").eq("user_id", user_id).limit(1).execute()
    if not prof_res.data:
        tg_send(chat_id, "Aún no tienes macros. Escribe /start para iniciar.")
        return
    prof = prof_res.data[0]

    # progreso del día (daily_log)
    today = str(date.today())
    log_res = sb.table("daily_log").select("total_kcal,total_p,total_c,total_f").eq("user_id", user_id).eq("day", today).limit(1).execute()
    if log_res.data:
        d = log_res.data[0]
        msg = (
            f"📊 RESUMEN {today}\n\n"
            f"🎯 Objetivo:\n"
            f"- Kcal: {int(prof['kcal_target'])}\n"
            f"- P: {int(prof['protein_g'])} g | C: {int(prof['carbs_g'])} g | F: {int(prof['fats_g'])} g\n\n"
            f"✅ Consumido:\n"
            f"- Kcal: {int(float(d['total_kcal']))}\n"
            f"- P: {round(float(d['total_p']),1)} g | C: {round(float(d['total_c']),1)} g | F: {round(float(d['total_f']),1)} g\n"
        )
    else:
        msg = (
            f"📊 RESUMEN {today}\n\n"
            f"🎯 Objetivo:\n"
            f"- Kcal: {int(prof['kcal_target'])}\n"
            f"- P: {int(prof['protein_g'])} g | C: {int(prof['carbs_g'])} g | F: {int(prof['fats_g'])} g\n\n"
            "✅ Consumido: 0 (aún no registraste comidas hoy)\n"
        )
    tg_send(chat_id, msg)


def handle_meal_select(chat_id: int, user_id: int, meal: str) -> None:
    st = get_state(user_id)
    data = st["data"] or {}
    data["current_meal"] = meal
    set_state(user_id, None, data)
    tg_send(chat_id, f"🍽️ Ok. Comida seleccionada: {meal}\nAhora escribe: <alimento> <gramos>\nEj: pollo cocido 180g")


def handle_log_food(chat_id: int, user_id: int, text: str) -> None:
    st = get_state(user_id)
    data = st["data"] or {}
    current_meal = data.get("current_meal") or "ALMUERZO"

    parsed = parse_food_grams(text)
    if not parsed:
        tg_send(chat_id, "No entendí. Ejemplo: pollo cocido 180g\nO escribe RESUMEN / " + " / ".join(sorted(MEAL_TYPES)))
        return

    food_name, grams = parsed

    # RPC log_food(p_user_id, p_meal_type, p_food_name, p_grams, p_day)
    today = str(date.today())
    try:
        sb.rpc(
            "log_food",
            {
                "p_user_id": user_id,
                "p_meal_type": current_meal,
                "p_food_name": food_name,
                "p_grams": grams,
                "p_day": today,
            },
        ).execute()
        tg_send(chat_id, f"✅ Registrado: {food_name} ({grams:g}g) en {current_meal}.\nEscribe RESUMEN para ver tu avance.")
    except Exception:
        tg_send(
            chat_id,
            f"❌ No encontré “{food_name}” en tu catálogo.\n"
            "Prueba con el nombre exacto (ej: pollo cocido, arroz, pepino).\n"
            "Más adelante habilitamos AGREGAR para cargar productos nuevos."
        )


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "status": "bot activo"}


@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()

    # Telegram update parsing
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return JSONResponse({"ok": True})

    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id"))
    from_user = msg.get("from") or {}
    display_name = normalize_text(from_user.get("first_name", "") + " " + from_user.get("last_name", ""))
    display_name = display_name.strip() or (from_user.get("username") or "Usuario")

    text = normalize_text(msg.get("text", ""))

    # Asegurar user_id
    user_id = get_or_create_user_id(chat_id, display_name)

    # Comandos básicos
    if text.startswith("/start"):
        handle_start(chat_id, user_id)
        return JSONResponse({"ok": True})

    if text.upper() == "AYUDA":
        tg_send(chat_id, help_text())
        return JSONResponse({"ok": True})

    if text.upper() == "RESUMEN":
        handle_summary(chat_id, user_id)
        return JSONResponse({"ok": True})

    # estado
    st = get_state(user_id)
    step = st["step"]

    # Si está esperando código
    if step == "WAIT_CODE":
        handle_code(chat_id, user_id, text)
        return JSONResponse({"ok": True})

    # Si está en onboarding
    if step and step.startswith("ONB_"):
        handle_onboarding(chat_id, user_id, text)
        return JSONResponse({"ok": True})

    # Si NO tiene perfil todavía, forzamos onboarding
    if not has_profile(user_id):
        tg_send(chat_id, "Primero configuramos tus macros. Escribe /start.")
        return JSONResponse({"ok": True})

    # Selección de comida
    up = text.upper()
    if up in MEAL_TYPES:
        handle_meal_select(chat_id, user_id, up)
        return JSONResponse({"ok": True})

    # Registrar alimento
    handle_log_food(chat_id, user_id, text)
    return JSONResponse({"ok": True})
