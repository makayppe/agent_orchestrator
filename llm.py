"""Capa de acceso al LLM: llamadas a Ollama con salida estructurada y modo mock."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Generator, Iterator, Optional, Tuple

from pydantic import BaseModel, ValidationError, field_validator

try:
    import ollama as _ollama
    _OLLAMA_OK = True
except ImportError:
    _OLLAMA_OK = False

# ── Injectable client for testing ─────────────────────────────────────────────
# Set llm._test_client = FakeClient() in tests to override Ollama calls.
_test_client = None


def _do_chat(**kwargs):
    """Wrapper that uses _test_client if set, otherwise uses the ollama library."""
    client = _test_client if _test_client is not None else _ollama
    return client.chat(**kwargs)


# ─── Decision model ───────────────────────────────────────────────────────────

class Decision(BaseModel):
    agente: str
    indicio_fraude: bool
    urgencia: int
    requiere_aclaracion: bool
    monto: Optional[float] = None
    razon: str

    @field_validator("agente", mode="before")
    @classmethod
    def normalizar_agente(cls, v: str) -> str:
        validos = {"FRAUDE", "REVERSOS", "PQR", "FUERA_DE_ALCANCE"}
        v_norm = str(v).upper().strip().replace(" ", "_")
        if v_norm in validos:
            return v_norm
        for op in validos:
            if op in v_norm:
                return op
        return "PQR"

    @field_validator("urgencia", mode="before")
    @classmethod
    def normalizar_urgencia(cls, v) -> int:
        try:
            return max(1, min(5, int(v)))
        except Exception:
            return 2

    @field_validator("razon", mode="before")
    @classmethod
    def truncar_razon(cls, v: str) -> str:
        return " ".join(str(v).split()[:20])


# ─── Emocion model ────────────────────────────────────────────────────────────

class Emocion(BaseModel):
    emocion: str     # neutral | confundido | frustrado | enojado | ansioso
    intensidad: str  # baja | media | alta
    razon: str

    @field_validator("emocion", mode="before")
    @classmethod
    def normalizar_emocion(cls, v: str) -> str:
        validos = {"neutral", "confundido", "frustrado", "enojado", "ansioso"}
        v_norm = str(v).lower().strip()
        if v_norm in validos:
            return v_norm
        for op in validos:
            if op in v_norm:
                return op
        return "neutral"

    @field_validator("intensidad", mode="before")
    @classmethod
    def normalizar_intensidad(cls, v: str) -> str:
        validos = {"baja", "media", "alta"}
        v_norm = str(v).lower().strip()
        return v_norm if v_norm in validos else "media"

    @field_validator("razon", mode="before")
    @classmethod
    def truncar_razon(cls, v: str) -> str:
        return " ".join(str(v).split()[:10])


# ─── System prompts (cargados desde prompts/*.md) ───────────────────────────────

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _cargar_prompt(nombre: str) -> str:
    return (_PROMPTS_DIR / f"{nombre}.md").read_text(encoding="utf-8").strip()


# La política ("definición legible" de reglas blandas) se inyecta en el prompt de
# decisión para que el modelo la conozca. Las reglas duras viven además en código.
_POLITICA = _cargar_prompt("politica")
_SISTEMA_DECISION = _cargar_prompt("decision") + "\n\n--- POLÍTICA DE ATENCIÓN ---\n" + _POLITICA
_SISTEMA_EMOCION = _cargar_prompt("emocion")
_SISTEMA_REDACCION = _cargar_prompt("redaccion")


# ─── Mock helpers ─────────────────────────────────────────────────────────────

def _mock_decision(mensaje: str) -> Decision:
    msg = mensaje.lower()

    indicio_fraude = any(w in msg for w in [
        "no reconozco", "no autoricé", "no autorizo",
        "robaron", "fraude", "clonaron", "clonada",
        "phishing", "suplantaron", "estaba dormido",
    ])

    if any(w in msg for w in ["ignora", "olvida", "pretende ser", "actúa como", "sin restricciones",
                               "reembolsa todo", "bypass", "jailbreak"]):
        return Decision(
            agente="FUERA_DE_ALCANCE", indicio_fraude=False, urgencia=1,
            requiere_aclaracion=False, monto=None,
            razon="Mensaje no corresponde a servicios bancarios",
        )

    if any(w in msg for w in ["doble cobro", "dos veces", "duplicado", "reverso", "devolver",
                               "me descontaron", "no me entregó", "no llegó", "suscripción",
                               "cajero", "transferencia", "cargos", "cobrado"]):
        agente, urgencia = "REVERSOS", 3
    elif indicio_fraude or any(w in msg for w in ["bloqueén", "bloqueen", "clonada", "no autorizado"]):
        agente, urgencia = "FRAUDE", 4
    elif any(w in msg for w in ["queja", "reclamo", "certificado", "información",
                                 "tasa", "producto", "servicio", "sucursal", "tramit"]):
        agente, urgencia = "PQR", 2
    elif not any(c.isalpha() for c in msg) or len(msg.split()) < 3:
        agente, urgencia = "PQR", 1
    elif any(w in msg for w in ["receta", "clima", "fútbol", "futbol", "chiste", "hola",
                                  "bandeja", "paisa"]):
        agente, urgencia = "FUERA_DE_ALCANCE", 1
    else:
        agente, urgencia = "PQR", 2

    requiere_aclaracion = len(msg.split()) < 6 and agente == "PQR"

    monto = None
    m = re.search(r"\$?\s*(\d[\d.,]+)", mensaje)
    if m:
        try:
            monto = float(m.group(1).replace(".", "").replace(",", "."))
        except Exception:
            pass

    return Decision(
        agente=agente, indicio_fraude=indicio_fraude, urgencia=urgencia,
        requiere_aclaracion=requiere_aclaracion, monto=monto,
        razon="Clasificación por palabras clave (modo mock)",
    )


def _mock_emocion(mensaje: str) -> Emocion:
    msg = mensaje.lower()
    if any(w in msg for w in ["muy enojado", "enojado", "furioso", "inaceptable", "indignado"]):
        emocion, intensidad = "enojado", "alta"
    elif any(w in msg for w in ["molesto", "frustrado", "harto", "cansado", "esto no puede"]):
        emocion, intensidad = "frustrado", "alta"
    elif any(w in msg for w in ["preocupado", "ansioso", "urgente", "necesito", "ayuda"]):
        emocion, intensidad = "ansioso", "media"
    elif any(w in msg for w in ["no entiendo", "confundido", "no sé", "por qué"]):
        emocion, intensidad = "confundido", "media"
    else:
        emocion, intensidad = "neutral", "baja"
    return Emocion(emocion=emocion, intensidad=intensidad, razon="Clasificación mock por palabras clave")


# ─── Funciones públicas ───────────────────────────────────────────────────────

def decidir(
    mensaje: str,
    modelo: str,
    mock: bool = False,
    temperatura: float = 0.4,
) -> Tuple[Decision, dict]:
    t0 = time.time()

    if mock:
        d = _mock_decision(mensaje)
        return d, {
            "latencia_ms": round((time.time() - t0) * 1000, 1),
            "tokens_prompt": 0, "tokens_respuesta": 0, "modelo": "mock",
            "es_fallback": False,
        }

    schema = Decision.model_json_schema()
    for intento in range(2):
        try:
            resp = _do_chat(
                model=modelo,
                messages=[
                    {"role": "system", "content": _SISTEMA_DECISION},
                    {"role": "user", "content": f"Mensaje del cliente: {mensaje}"},
                ],
                format=schema,
                options={"temperature": temperatura},
            )
            d = Decision.model_validate_json(resp.message.content)
            return d, {
                "latencia_ms": round((time.time() - t0) * 1000, 1),
                "tokens_prompt": getattr(resp, "prompt_eval_count", 0) or 0,
                "tokens_respuesta": getattr(resp, "eval_count", 0) or 0,
                "modelo": modelo, "es_fallback": False,
            }
        except (ValidationError, Exception) as e:
            if intento == 0:
                time.sleep(0.3)
                continue
            d = _mock_decision(mensaje)
            return d, {
                "latencia_ms": round((time.time() - t0) * 1000, 1),
                "tokens_prompt": 0, "tokens_respuesta": 0,
                "modelo": f"{modelo}(fallback-mock)", "error": str(e)[:120],
                "es_fallback": True,
            }


def decidir_paralelo(
    mensaje: str,
    modelo: str,
    n: int,
    mock: bool = False,
    temperatura: float = 0.4,
) -> list[Tuple[Decision, dict]]:
    """Run n decision calls concurrently and return all results."""
    if n <= 1:
        return [decidir(mensaje, modelo, mock, temperatura)]

    resultados = []
    with ThreadPoolExecutor(max_workers=n) as executor:
        futuros = [executor.submit(decidir, mensaje, modelo, mock, temperatura) for _ in range(n)]
        for fut in futuros:
            try:
                resultados.append(fut.result())
            except Exception:
                pass

    if not resultados:
        d = _mock_decision(mensaje) if mock else _mock_decision(mensaje)
        resultados = [(d, {"latencia_ms": 0, "tokens_prompt": 0, "tokens_respuesta": 0, "modelo": "fallback-mock"})]
    return resultados


def clasificar_emocion_raw(
    mensaje: str,
    modelo: str,
    mock: bool = False,
    simulated_delay_ms: int = 0,
) -> Emocion:
    """Classify emotion. simulated_delay_ms forces a delay (for demo timeout testing)."""
    if simulated_delay_ms > 0:
        time.sleep(simulated_delay_ms / 1000)

    if mock:
        return _mock_emocion(mensaje)

    schema = Emocion.model_json_schema()
    try:
        resp = _do_chat(
            model=modelo,
            messages=[
                {"role": "system", "content": _SISTEMA_EMOCION},
                {"role": "user", "content": f"Mensaje del cliente: {mensaje}"},
            ],
            format=schema,
            options={"temperature": 0.3},
        )
        return Emocion.model_validate_json(resp.message.content)
    except Exception:
        return _mock_emocion(mensaje)


def clasificar_emocion(
    mensaje: str,
    modelo: str,
    mock: bool = False,
    timeout_ms: int = 1200,
    simulated_delay_ms: int = 0,
) -> Tuple[Optional[Emocion], bool]:
    """Run emotion classification with a timeout.

    Returns (Emocion | None, timed_out).
    If timed_out is True, the caller should use the previous turn's emotion.
    """
    # Sin `with`: su __exit__ hace shutdown(wait=True) y esperaría al hilo igual,
    # anulando el timeout. Cerramos sin esperar y cancelando el futuro.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        fut = executor.submit(clasificar_emocion_raw, mensaje, modelo, mock, simulated_delay_ms)
        try:
            return fut.result(timeout=timeout_ms / 1000), False
        except Exception:
            return None, True
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def redactar(
    mensaje_cliente: str,
    agentes_usados: list[str],
    resumen_acciones: str,
    modelo: str,
    mock: bool = False,
) -> Tuple[str, dict]:
    t0 = time.time()

    if mock:
        texto = (
            f"Hemos recibido su solicitud y tomado las medidas necesarias. "
            f"Los servicios activados fueron: {', '.join(agentes_usados)}. "
            "En breve recibirá una notificación con el resultado de su caso."
        )
        return texto, {
            "latencia_ms": round((time.time() - t0) * 1000, 1),
            "tokens_prompt": 0, "tokens_respuesta": 0,
        }

    prompt = (
        f"Mensaje del cliente:\n{mensaje_cliente}\n\n"
        f"Acciones realizadas:\n{resumen_acciones}"
    )
    try:
        resp = _do_chat(
            model=modelo,
            messages=[
                {"role": "system", "content": _SISTEMA_REDACCION},
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.5},
        )
        return resp.message.content.strip(), {
            "latencia_ms": round((time.time() - t0) * 1000, 1),
            "tokens_prompt": getattr(resp, "prompt_eval_count", 0) or 0,
            "tokens_respuesta": getattr(resp, "eval_count", 0) or 0,
        }
    except Exception as e:
        fallback = f"Su solicitud ha sido procesada exitosamente. {resumen_acciones}"
        return fallback, {
            "latencia_ms": round((time.time() - t0) * 1000, 1),
            "tokens_prompt": 0, "tokens_respuesta": 0, "error": str(e)[:120],
        }


def redactar_streaming(
    mensaje_cliente: str,
    agentes_usados: list[str],
    resumen_acciones: str,
    modelo: str,
    mock: bool = False,
) -> Iterator[str]:
    """Yield response text chunks as they arrive from the model."""
    if mock:
        yield (
            f"Hemos recibido su solicitud y tomado las medidas necesarias. "
            f"Los servicios activados fueron: {', '.join(agentes_usados)}. "
            "En breve recibirá una notificación con el resultado de su caso."
        )
        return

    prompt = (
        f"Mensaje del cliente:\n{mensaje_cliente}\n\n"
        f"Acciones realizadas:\n{resumen_acciones}"
    )
    try:
        stream = _do_chat(
            model=modelo,
            messages=[
                {"role": "system", "content": _SISTEMA_REDACCION},
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.5},
            stream=True,
        )
        for chunk in stream:
            texto = getattr(chunk.message, "content", "") or ""
            if texto:
                yield texto
    except Exception as e:
        yield f"Su solicitud ha sido procesada. {resumen_acciones}"


def verificar_ollama(modelo_decision: str, modelo_redaccion: str) -> dict:
    if not _OLLAMA_OK:
        return {
            "ok": False, "modelos_disponibles": [],
            "errores": ["Librería 'ollama' no instalada. Ejecute: pip install ollama"],
        }
    try:
        lista = _ollama.list()
        disponibles = [m.model for m in lista.models]

        faltantes = []
        for modelo in {modelo_decision, modelo_redaccion}:
            nombre_base = modelo.split(":")[0]
            if not any(nombre_base in d for d in disponibles):
                faltantes.append(modelo)

        return {
            "ok": len(faltantes) == 0,
            "modelos_disponibles": disponibles,
            "errores": (
                [f"Modelos no descargados: {', '.join(faltantes)} — ejecute: ollama pull <modelo>"]
                if faltantes else []
            ),
        }
    except Exception as e:
        return {
            "ok": False, "modelos_disponibles": [],
            "errores": [f"Ollama no responde. Ejecute: ollama serve\n  Detalle: {e}"],
        }
