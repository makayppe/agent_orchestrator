"""Orquestador: decisión LLM → reglas de negocio → agentes."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agentes import AGENTES, identificar_cliente
from llm import Decision, Emocion, clasificar_emocion_raw, decidir, decidir_paralelo, redactar


# ─── Session state ────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    turno: int = 0
    historial: list[dict] = field(default_factory=list)
    # Identidad del cliente (None hasta que se identifique en el chat)
    cliente_id: Optional[str] = None
    cliente_nombre: Optional[str] = None
    cliente_cuenta: Optional[str] = None
    solicitud_pendiente: Optional[str] = None
    emocion_previa: str = "neutral"
    intensidad_previa: str = "baja"
    fallbacks_emocion_consecutivos: int = 0
    contactos_mismo_caso: int = 0
    escalado: bool = False
    razon_escalado: str = ""
    # Stored agent results for follow-up idempotency
    reverso_id: Optional[str] = None
    reverso_monto: Optional[float] = None
    reverso_comercio: Optional[str] = None
    caso_id: Optional[str] = None
    bloqueo_id: Optional[str] = None
    hechos: dict = field(default_factory=dict)
    resultados_agentes_guardados: list[dict] = field(default_factory=list)


# ─── Resultado ────────────────────────────────────────────────────────────────

@dataclass
class ResultadoProcesamiento:
    # Paso 1: decisiones
    decisiones: list[Decision] = field(default_factory=list)
    metricas_decision: list[dict] = field(default_factory=list)
    agente_ganador: str = ""
    acuerdo: float = 0.0

    # Paso 1b: emoción
    emocion: Optional[Emocion] = None
    emocion_fuente: str = "t"   # "t", "t-1", o "desconocida"

    # Paso 2: reglas
    agentes_a_ejecutar: list[str] = field(default_factory=list)
    reglas_disparadas: list[dict] = field(default_factory=list)
    escalado_humano: bool = False
    razon_escalado: str = ""
    requiere_aclaracion: bool = False

    # Flags especiales
    manipulacion_detectada: bool = False
    es_seguimiento: bool = False
    guardia_activada: bool = False
    requiere_identificacion: bool = False
    identificacion_ok: bool = False

    # Paso 3: agentes
    agentes_ejecutados: list[str] = field(default_factory=list)
    resultados_agentes: list[dict] = field(default_factory=list)

    # Paso 4: respuesta
    respuesta_final: str = ""
    metricas_redaccion: dict = field(default_factory=dict)

    def a_dict(self) -> dict:
        return {
            "decisiones": [d.model_dump() for d in self.decisiones],
            "metricas_decision": self.metricas_decision,
            "agente_ganador": self.agente_ganador,
            "acuerdo": self.acuerdo,
            "emocion": self.emocion.model_dump() if self.emocion else None,
            "emocion_fuente": self.emocion_fuente,
            "agentes_a_ejecutar": self.agentes_a_ejecutar,
            "reglas_disparadas": self.reglas_disparadas,
            "escalado_humano": self.escalado_humano,
            "razon_escalado": self.razon_escalado,
            "requiere_aclaracion": self.requiere_aclaracion,
            "manipulacion_detectada": self.manipulacion_detectada,
            "es_seguimiento": self.es_seguimiento,
            "guardia_activada": self.guardia_activada,
            "requiere_identificacion": self.requiere_identificacion,
            "identificacion_ok": self.identificacion_ok,
            "agentes_ejecutados": self.agentes_ejecutados,
            "resultados_agentes": self.resultados_agentes,
            "respuesta_final": self.respuesta_final,
            "metricas_redaccion": self.metricas_redaccion,
        }

    @classmethod
    def desde_dict(cls, d: dict) -> "ResultadoProcesamiento":
        r = cls()
        r.decisiones = [Decision(**dd) for dd in d["decisiones"]]
        r.metricas_decision = d["metricas_decision"]
        r.agente_ganador = d["agente_ganador"]
        r.acuerdo = d["acuerdo"]
        if d.get("emocion"):
            r.emocion = Emocion(**d["emocion"])
        r.emocion_fuente = d.get("emocion_fuente", "t")
        r.agentes_a_ejecutar = d["agentes_a_ejecutar"]
        r.reglas_disparadas = d["reglas_disparadas"]
        r.escalado_humano = d["escalado_humano"]
        r.razon_escalado = d["razon_escalado"]
        r.requiere_aclaracion = d["requiere_aclaracion"]
        r.manipulacion_detectada = d.get("manipulacion_detectada", False)
        r.es_seguimiento = d.get("es_seguimiento", False)
        r.guardia_activada = d.get("guardia_activada", False)
        r.requiere_identificacion = d.get("requiere_identificacion", False)
        r.identificacion_ok = d.get("identificacion_ok", False)
        r.agentes_ejecutados = d["agentes_ejecutados"]
        r.resultados_agentes = d["resultados_agentes"]
        r.respuesta_final = d["respuesta_final"]
        r.metricas_redaccion = d["metricas_redaccion"]
        return r


# ─── Helper functions ─────────────────────────────────────────────────────────

_PALABRAS_MANIPULACION = [
    "ignora tus", "ignora todas", "olvida tus reglas", "olvida las reglas",
    "eres libre", "sin restricciones", "reembolsa todo", "dame todo el dinero",
    "bypass", "jailbreak", "actúa sin reglas", "sin límites",
]


def es_manipulacion(mensaje: str) -> bool:
    msg = mensaje.lower()
    return any(p in msg for p in _PALABRAS_MANIPULACION)


def _parece_solicitud(mensaje: str) -> bool:
    """Heurística: un mensaje con ≥3 palabras parece una solicitud real (no solo un ID)."""
    return len(mensaje.split()) >= 3


_PALABRAS_SEGUIMIENTO = [
    "cuánto", "cuanto", "demora", "reverso", "mi caso", "qué pasó", "que paso",
    "cuándo", "cuando", "estado", "me devuelven", "mi reverso", "mi ticket",
    "ya lo", "lo procesaron", "alguna novedad", "novedad",
]


def es_seguimiento(mensaje: str, session: SessionState) -> bool:
    if session.turno < 1:
        return False
    if not (session.reverso_id or session.caso_id or session.resultados_agentes_guardados):
        return False
    msg = mensaje.lower()
    palabras = msg.split()
    return len(palabras) <= 12 and any(p in msg for p in _PALABRAS_SEGUIMIENTO)


def _extraer_hechos(resultados_agentes: list[dict]) -> dict:
    hechos: dict = {"tickets": set(), "montos": set(), "plazos": set()}
    for resultado in resultados_agentes:
        for herr in resultado.get("herramientas", []):
            for campo in ("ticket", "caso_id", "radicado"):
                if v := herr.get(campo):
                    hechos["tickets"].add(v)
            if m := herr.get("monto"):
                hechos["montos"].add(float(m))
            if t := herr.get("tiempo_estimado"):
                hechos["plazos"].add(t)
    return hechos


# Tickets con formato banco (REV-xxxxxxxx, BLQ-xxxxxxxx, …)
_TICKET_PATTERN = re.compile(r"\b([A-Z]+-[0-9A-Fa-f]{8})\b")
# Cualquier secuencia "larga" de dígitos que el redactor podría presentar como
# número de caso/ticket (ej. "ticket 123456"). Umbral: ≥ 5 dígitos.
_NUM_LARGO_PATTERN = re.compile(r"\b\d{5,}\b")


def _normalizar_monto(valor: str) -> Optional[float]:
    """Convierte '87.500' / '87,500' / '87500' a float, o None si no se puede."""
    limpio = valor.replace(".", "").replace(",", "")
    try:
        return float(limpio)
    except ValueError:
        return None


def _guardia_redactor(texto: str, hechos: dict) -> tuple[Optional[str], bool]:
    """Verifica que todo ID/número de caso del texto esté entre los hechos conocidos.

    Cubre tanto tickets con formato (REV-xxxxxxxx) como números sueltos largos
    (ej. 'ticket 123456'). Un número suelto se considera legítimo solo si coincide
    con un monto conocido; cualquier otro número largo se trata como inventado.

    Returns (texto_verificado | None, guardia_activada).
    Returns (None, True) si se detecta un ticket/número inventado.
    """
    # 1) Tickets con formato banco
    tickets_en_texto = {t.upper() for t in _TICKET_PATTERN.findall(texto)}
    tickets_conocidos = {t.upper() for t in hechos.get("tickets", set())}
    if tickets_en_texto - tickets_conocidos:
        return None, True

    # 2) Números largos sueltos → solo válidos si son un monto conocido
    montos_conocidos = {int(m) for m in hechos.get("montos", set())}
    for num in _NUM_LARGO_PATTERN.findall(texto):
        # Ignorar los que ya son parte de un ticket con formato
        if any(num in t for t in tickets_en_texto):
            continue
        valor = _normalizar_monto(num)
        if valor is None or int(valor) not in montos_conocidos:
            return None, True

    return texto, False


def _mayoria(decisiones: list[Decision]) -> tuple[str, float]:
    conteo = Counter(d.agente for d in decisiones)
    ganador, votos = conteo.most_common(1)[0]
    return ganador, votos / len(decisiones)


def _registrar_auditoria(path: str, session: SessionState, mensaje: str, r: ResultadoProcesamiento) -> None:
    """Escribe una línea JSON por turno con lo decidido (registro para riesgo).

    Determinista y legible: decisión, reglas disparadas, escalado y tickets.
    """
    registro = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session.id,
        "turno": session.turno,
        "cliente_id": session.cliente_id,
        "mensaje": mensaje[:200],
        "agente_ganador": r.agente_ganador,
        "acuerdo": r.acuerdo,
        "agentes_ejecutados": r.agentes_ejecutados,
        "reglas_disparadas": [reg["num"] for reg in r.reglas_disparadas if reg.get("disparada")],
        "escalado_humano": r.escalado_humano,
        "razon_escalado": r.razon_escalado,
        "guardia_activada": r.guardia_activada,
        "tickets": sorted(session.hechos.get("tickets", [])) if session.hechos else [],
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except Exception:
        # La auditoría nunca debe tumbar el turno
        pass


# ─── Orquestador ─────────────────────────────────────────────────────────────

class Orquestador:
    def __init__(
        self,
        modelo_decision: str,
        modelo_redaccion: str,
        mock: bool = False,
        corridas: int = 1,
        umbral_acuerdo: float = 0.67,
        umbral_monto: float = 2_000_000,
        temperatura: float = 0.4,
        ronda: int = 3,
        cliente_id: str = "C001",
        emotion_timeout_ms: int = 1200,
        emotion_delay_ms: int = 0,
        decision_timeout_ms: int = 8000,
        requiere_identificacion: bool = False,
        audit_path: Optional[str] = None,
    ):
        self.modelo_decision = modelo_decision
        self.modelo_redaccion = modelo_redaccion
        self.mock = mock
        self.corridas = corridas
        self.umbral_acuerdo = umbral_acuerdo
        self.umbral_monto = umbral_monto
        self.temperatura = temperatura
        self.ronda = ronda
        self.cliente_id = cliente_id
        self.emotion_timeout_ms = emotion_timeout_ms
        self.emotion_delay_ms = emotion_delay_ms
        self.decision_timeout_ms = decision_timeout_ms
        self.requiere_identificacion = requiere_identificacion
        self.audit_path = audit_path

    def procesar(self, mensaje: str, session: Optional[SessionState] = None) -> ResultadoProcesamiento:
        if session is None:
            session = SessionState()
        r = self._procesar_interno(mensaje, session)
        if self.audit_path:
            _registrar_auditoria(self.audit_path, session, mensaje, r)
        return r

    def _procesar_interno(self, mensaje: str, session: SessionState) -> ResultadoProcesamiento:
        session.turno += 1
        r = ResultadoProcesamiento()

        # ── REGLA 7 (máx. prioridad): Manipulación ───────────────────────────
        if es_manipulacion(mensaje):
            r.manipulacion_detectada = True
            r.escalado_humano = True
            r.razon_escalado = "Intento de manipulación detectado en el mensaje"
            r.respuesta_final = (
                "Esta solicitud ha sido marcada para revisión de seguridad. "
                "Un especialista revisará su caso."
            )
            r.reglas_disparadas.append({
                "num": 1, "desc": "manipulación detectada → bloqueo inmediato (prioridad máxima)",
                "disparada": True, "detalle": "Se detectaron instrucciones para eludir reglas del sistema",
            })
            session.escalado = True
            session.historial.append({"turno": session.turno, "rol": "cliente", "texto": mensaje[:100]})
            session.historial.append({"turno": session.turno, "rol": "sistema", "texto": r.respuesta_final})
            return r

        # ── Identificación del cliente (solo en modo chat) ───────────────────
        if self.requiere_identificacion and not session.cliente_id:
            continuar = self._gate_identificacion(mensaje, r, session)
            if not continuar:
                return r
            # Identificado con solicitud pendiente: procesar esa solicitud ahora
            mensaje = continuar

        # ── REGLA 8: Sesión ya escalada ──────────────────────────────────────
        if session.escalado:
            r.escalado_humano = True
            r.razon_escalado = "Sesión previamente escalada a atención humana"
            r.respuesta_final = (
                "Su caso está siendo atendido por un asesor especializado. "
                "No se requieren más acciones de su parte."
            )
            r.reglas_disparadas.append({
                "num": 3, "desc": "sesión ya escalada → mantener en atención humana",
                "disparada": True, "detalle": session.razon_escalado,
            })
            session.contactos_mismo_caso += 1
            session.historial.append({"turno": session.turno, "rol": "cliente", "texto": mensaje[:100]})
            session.historial.append({"turno": session.turno, "rol": "sistema", "texto": r.respuesta_final})
            return r

        # ── Seguimiento: usar estado guardado ────────────────────────────────
        if es_seguimiento(mensaje, session) and self.ronda == 3:
            r.es_seguimiento = True
            # Un seguimiento sobre un caso existente SÍ cuenta como contacto.
            session.contactos_mismo_caso += 1
            r = self._procesar_seguimiento(mensaje, r, session)
            session.historial.append({"turno": session.turno, "rol": "cliente", "texto": mensaje[:100]})
            session.historial.append({"turno": session.turno, "rol": "sistema", "texto": r.respuesta_final})
            return r

        # ── Paso 1: Decisiones + emoción en paralelo ─────────────────────────
        # No usamos `with`: su __exit__ hace shutdown(wait=True) y anularía el
        # timeout de emoción (esperaría al hilo igual). Cerramos sin esperar.
        decision_results = []
        emocion_raw = None
        emocion_timed_out = False

        executor = ThreadPoolExecutor(max_workers=self.corridas + 1)
        try:
            fut_decisions = [
                executor.submit(decidir, mensaje, self.modelo_decision, self.mock, self.temperatura)
                for _ in range(self.corridas)
            ]
            fut_emocion = executor.submit(
                clasificar_emocion_raw,
                mensaje, self.modelo_decision, self.mock, self.emotion_delay_ms,
            )

            for fut in fut_decisions:
                try:
                    decision_results.append(fut.result(timeout=self.decision_timeout_ms / 1000))
                except Exception:
                    pass

            try:
                emocion_raw = fut_emocion.result(timeout=self.emotion_timeout_ms / 1000)
                emocion_timed_out = False
            except Exception:
                emocion_timed_out = True
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if not decision_results:
            from llm import _mock_decision
            decision_results = [(_mock_decision(mensaje), {"latencia_ms": 0, "tokens_prompt": 0, "tokens_respuesta": 0, "modelo": "fallback-mock", "es_fallback": True})]

        for d, m in decision_results:
            r.decisiones.append(d)
            r.metricas_decision.append(m)

        r.agente_ganador, r.acuerdo = _mayoria(r.decisiones)

        # ── FAIL-SAFE: si el clasificador de decisión no está disponible (todas
        # las corridas cayeron a respaldo por palabras clave), NO se ejecutan
        # agentes con esa clasificación: se escala a humano.
        clasificador_caido = not self.mock and all(
            m.get("es_fallback") for m in r.metricas_decision
        )
        if clasificador_caido:
            r.escalado_humano = True
            r.razon_escalado = "Clasificador de decisión no disponible — escalado por seguridad (fail-safe)"
            r.reglas_disparadas.append({
                "num": 2, "desc": "fail-safe: clasificador de decisión caído → escalar",
                "disparada": True, "detalle": "todas las corridas usaron respaldo por palabras clave",
            })
            r.respuesta_final = (
                "Estamos teniendo una dificultad técnica para procesar su solicitud de forma segura. "
                "Un asesor revisará su caso en breve."
            )
            session.escalado = True
            session.razon_escalado = r.razon_escalado
            self._actualizar_session_emocion(r, session)
            session.historial.append({"turno": session.turno, "rol": "cliente", "texto": mensaje[:100]})
            session.historial.append({"turno": session.turno, "rol": "sistema", "texto": r.respuesta_final})
            return r

        # ── Emoción: fallback a t-1 si timeout ───────────────────────────────
        if emocion_timed_out:
            session.fallbacks_emocion_consecutivos += 1
            if session.fallbacks_emocion_consecutivos >= 2:
                r.emocion = Emocion(emocion="desconocida", intensidad="media", razon="Demasiados timeouts consecutivos")
                r.emocion_fuente = "desconocida"
            else:
                r.emocion = Emocion(
                    emocion=session.emocion_previa,
                    intensidad=session.intensidad_previa,
                    razon="Timeout — usando emoción del turno anterior",
                )
                r.emocion_fuente = "t-1"
        else:
            session.fallbacks_emocion_consecutivos = 0
            r.emocion = emocion_raw
            r.emocion_fuente = "t"

        if self.ronda == 1:
            r.agentes_a_ejecutar = [r.agente_ganador]
            self._actualizar_session_emocion(r, session)
            return r

        # ── Paso 2: Reglas de negocio ────────────────────────────────────────
        agentes = [r.agente_ganador]
        decision = r.decisiones[0]

        # Regla 6: "no reconozco" → override indicio_fraude
        _FRASES_NO_RECONOZCO = ["no reconozco", "no autoricé", "no autorizo"]
        hay_no_reconozco = any(f in mensaje.lower() for f in _FRASES_NO_RECONOZCO)
        if hay_no_reconozco:
            for d in r.decisiones:
                if not d.indicio_fraude:
                    d.indicio_fraude = True
        r.reglas_disparadas.append({
            "num": 4,
            "desc": "'no reconozco/autoricé' → activa indicio_fraude",
            "disparada": hay_no_reconozco,
            "detalle": "frase de no reconocimiento detectada" if hay_no_reconozco else "",
        })

        hay_fraude = any(d.indicio_fraude for d in r.decisiones)
        max_urgencia = max(d.urgencia for d in r.decisiones)
        monto = max((d.monto or 0) for d in r.decisiones)

        # Regla 1: indicio_fraude → ejecutar también FRAUDE
        regla1 = hay_fraude and "FRAUDE" not in agentes
        if regla1:
            agentes.append("FRAUDE")
        r.reglas_disparadas.append({
            "num": 5, "desc": "indicio_fraude → ejecutar agente FRAUDE",
            "disparada": regla1,
            "detalle": f"indicio_fraude={'sí' if hay_fraude else 'no'}" if regla1 else "",
        })

        # Regla 2: urgencia ≥ 4 y monto > umbral → escalar a humano
        regla2 = max_urgencia >= 4 and monto > self.umbral_monto
        if regla2:
            r.escalado_humano = True
            r.razon_escalado = f"Urgencia {max_urgencia}/5 y monto ${monto:,.0f} > umbral ${self.umbral_monto:,.0f}"
        r.reglas_disparadas.append({
            "num": 6, "desc": f"urgencia ≥ 4 y monto > ${self.umbral_monto:,.0f} → escalar",
            "disparada": regla2,
            "detalle": f"urgencia={max_urgencia}, monto=${monto:,.0f}" if regla2 else "",
        })

        # Regla 3: requiere_aclaracion → preguntar al cliente
        regla3 = decision.requiere_aclaracion
        if regla3:
            r.requiere_aclaracion = True
            r.respuesta_final = (
                "Para poder ayudarle mejor, ¿podría darnos más detalles sobre su situación? "
                "Por ejemplo: fecha, comercio o monto involucrado."
            )
        r.reglas_disparadas.append({
            "num": 7, "desc": "requiere_aclaracion → preguntar al cliente en lugar de enrutar",
            "disparada": regla3, "detalle": "mensaje demasiado vago" if regla3 else "",
        })

        # Regla 4: autoconsistencia por debajo del umbral → escalar
        regla4 = self.corridas > 1 and r.acuerdo < self.umbral_acuerdo
        if regla4:
            r.escalado_humano = True
            r.razon_escalado = f"Acuerdo {r.acuerdo:.0%} < umbral {self.umbral_acuerdo:.0%} en {self.corridas} corridas"
        r.reglas_disparadas.append({
            "num": 8, "desc": f"autoconsistencia < {self.umbral_acuerdo:.0%} → escalar",
            "disparada": regla4, "detalle": f"acuerdo={r.acuerdo:.0%}" if regla4 else "",
        })

        # Contar el contacto solo si es un contacto real sobre un caso: enruta a
        # un agente de caso (no FUERA_DE_ALCANCE) y no es una petición de
        # aclaración. Así saludos y mensajes vagos no inflan la Regla 5.
        if r.agente_ganador != "FUERA_DE_ALCANCE" and not r.requiere_aclaracion:
            session.contactos_mismo_caso += 1

        # Regla 5: enojo/frustración alta + ≥2 contactos → escalar
        emocion_actual = r.emocion.emocion if r.emocion else session.emocion_previa
        intensidad_actual = r.emocion.intensidad if r.emocion else session.intensidad_previa
        regla5 = (
            emocion_actual in ("enojado", "frustrado")
            and intensidad_actual == "alta"
            and session.contactos_mismo_caso >= 2
        )
        if regla5:
            r.escalado_humano = True
            r.razon_escalado = (
                f"Emoción '{emocion_actual}' (intensidad alta) en contacto "
                f"#{session.contactos_mismo_caso} sobre el mismo caso"
            )
        r.reglas_disparadas.append({
            "num": 9,
            "desc": "enojo/frustración alta + ≥2 contactos → escalar a humano",
            "disparada": regla5,
            "detalle": f"emoción={emocion_actual}, intensidad={intensidad_actual}, contactos={session.contactos_mismo_caso}" if regla5 else "",
        })

        # Sort rules by number
        r.reglas_disparadas.sort(key=lambda x: x["num"])
        r.agentes_a_ejecutar = agentes

        if regla3:
            self._actualizar_session_emocion(r, session)
            return r
        if self.ronda == 2:
            self._actualizar_session_emocion(r, session)
            return r

        # ── Paso 3: Ejecución de agentes ─────────────────────────────────────
        resumenes = []
        for nombre in agentes:
            if nombre == "FUERA_DE_ALCANCE":
                resultado = {
                    "agente": "FUERA_DE_ALCANCE", "herramientas": [],
                    "resumen": "Mensaje fuera del dominio bancario — no se toman acciones",
                }
            elif nombre in AGENTES:
                cid = session.cliente_id or self.cliente_id
                resultado = AGENTES[nombre].ejecutar(mensaje, cid, session=session)
            else:
                continue
            r.resultados_agentes.append(resultado)
            r.agentes_ejecutados.append(nombre)
            resumenes.append(resultado["resumen"])

        # Store results in session for follow-up idempotency
        session.resultados_agentes_guardados = r.resultados_agentes
        hechos = _extraer_hechos(r.resultados_agentes)
        session.hechos = {k: list(v) if isinstance(v, set) else v for k, v in hechos.items()}
        hechos_set = hechos  # keep as sets for guard

        # ── Paso 4: Redacción con guardia ────────────────────────────────────
        if r.escalado_humano:
            r.respuesta_final = (
                f"Su caso ha sido escalado para atención especializada. "
                f"Motivo: {r.razon_escalado}. "
                "Un asesor se comunicará con usted en los próximos 30 minutos."
            )
            r.metricas_redaccion = {"latencia_ms": 0, "tokens_prompt": 0, "tokens_respuesta": 0}
            session.escalado = True
        else:
            texto_generado, metricas = redactar(
                mensaje, r.agentes_ejecutados, "; ".join(resumenes),
                self.modelo_redaccion, mock=self.mock,
            )
            texto_verificado, activada = _guardia_redactor(texto_generado, hechos_set)
            if activada:
                r.guardia_activada = True
                r.respuesta_final = (
                    f"Su solicitud fue procesada. {'; '.join(resumenes)}. "
                    "Recibirá una notificación de seguimiento."
                )
            else:
                r.respuesta_final = texto_verificado
            r.metricas_redaccion = metricas

        self._actualizar_session_emocion(r, session)
        session.historial.append({"turno": session.turno, "rol": "cliente", "texto": mensaje[:100]})
        session.historial.append({"turno": session.turno, "rol": "sistema", "texto": r.respuesta_final[:200]})
        return r

    def _procesar_seguimiento(
        self, mensaje: str, r: ResultadoProcesamiento, session: SessionState
    ) -> ResultadoProcesamiento:
        """Handle follow-up questions using stored session state."""
        r.resultados_agentes = session.resultados_agentes_guardados
        r.agentes_ejecutados = [res["agente"] for res in r.resultados_agentes]
        r.agentes_a_ejecutar = r.agentes_ejecutados

        # Run emotion in background (with timeout) — still needed for rule 5.
        # Sin `with`: evitamos que shutdown(wait=True) anule el timeout.
        emocion_raw = None
        emocion_timed_out = False
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            fut_em = executor.submit(
                clasificar_emocion_raw,
                mensaje, self.modelo_decision, self.mock, self.emotion_delay_ms,
            )
            try:
                emocion_raw = fut_em.result(timeout=self.emotion_timeout_ms / 1000)
            except Exception:
                emocion_timed_out = True
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if emocion_timed_out:
            session.fallbacks_emocion_consecutivos += 1
            if session.fallbacks_emocion_consecutivos >= 2:
                r.emocion = Emocion(emocion="desconocida", intensidad="media", razon="Demasiados timeouts")
                r.emocion_fuente = "desconocida"
            else:
                r.emocion = Emocion(
                    emocion=session.emocion_previa,
                    intensidad=session.intensidad_previa,
                    razon="Timeout — usando emoción del turno anterior",
                )
                r.emocion_fuente = "t-1"
        else:
            session.fallbacks_emocion_consecutivos = 0
            r.emocion = emocion_raw
            r.emocion_fuente = "t"

        # Check rule 5 on follow-up (anger + contacts ≥ 2)
        emocion_actual = r.emocion.emocion if r.emocion else session.emocion_previa
        intensidad_actual = r.emocion.intensidad if r.emocion else session.intensidad_previa
        regla5 = (
            emocion_actual in ("enojado", "frustrado")
            and intensidad_actual == "alta"
            and session.contactos_mismo_caso >= 2
        )
        if regla5:
            r.escalado_humano = True
            r.razon_escalado = (
                f"Emoción '{emocion_actual}' (intensidad alta) en contacto "
                f"#{session.contactos_mismo_caso} sobre el mismo caso"
            )
            r.reglas_disparadas.append({
                "num": 9,
                "desc": "enojo/frustración alta + ≥2 contactos → escalar a humano",
                "disparada": True,
                "detalle": f"emoción={emocion_actual}, intensidad={intensidad_actual}, contactos={session.contactos_mismo_caso}",
            })

        if r.escalado_humano:
            r.respuesta_final = (
                f"Su caso ha sido escalado para atención especializada. "
                f"Motivo: {r.razon_escalado}. "
                "Un asesor se comunicará con usted en los próximos 30 minutos."
            )
            session.escalado = True
        else:
            # Build response from stored facts
            hechos_set = {k: set(v) if isinstance(v, list) else v for k, v in session.hechos.items()}
            resumenes = [res["resumen"] for res in r.resultados_agentes]
            texto_generado, metricas = redactar(
                mensaje, r.agentes_ejecutados, "; ".join(resumenes),
                self.modelo_redaccion, mock=self.mock,
            )
            texto_verificado, activada = _guardia_redactor(texto_generado, hechos_set)
            if activada:
                r.guardia_activada = True
                r.respuesta_final = (
                    f"Su solicitud fue procesada. {'; '.join(resumenes)}. "
                    "Recibirá una notificación de seguimiento."
                )
            else:
                r.respuesta_final = texto_verificado
            r.metricas_redaccion = metricas

        self._actualizar_session_emocion(r, session)
        return r

    def _gate_identificacion(self, mensaje: str, r: ResultadoProcesamiento, session: SessionState):
        """Puerta de identificación del cliente.

        Devuelve el mensaje a procesar (str) si el cliente se identificó y hay una
        solicitud pendiente; devuelve None si pidió identificación o solo saludó
        (en cuyo caso el llamador debe terminar el turno).
        """
        cliente = identificar_cliente(mensaje)
        if cliente:
            session.cliente_id = cliente["id"]
            session.cliente_nombre = cliente["nombre"]
            session.cliente_cuenta = cliente["cuenta"]
            r.identificacion_ok = True
            if session.solicitud_pendiente:
                pendiente = session.solicitud_pendiente
                session.solicitud_pendiente = None
                return pendiente  # procesar la solicitud guardada ahora
            primer_nombre = session.cliente_nombre.split()[0]
            r.respuesta_final = (
                f"Gracias, {primer_nombre}. Su identidad fue verificada. "
                "¿En qué puedo ayudarle hoy?"
            )
            session.historial.append({"turno": session.turno, "rol": "cliente", "texto": mensaje[:100]})
            session.historial.append({"turno": session.turno, "rol": "sistema", "texto": r.respuesta_final})
            return None

        # No se encontró identificador → pedirlo y recordar la solicitud
        if _parece_solicitud(mensaje):
            session.solicitud_pendiente = mensaje
        r.requiere_identificacion = True
        r.respuesta_final = (
            "Para ayudarle de forma segura, ¿me confirma su número de documento "
            "o los últimos 4 dígitos de su cuenta?"
        )
        session.historial.append({"turno": session.turno, "rol": "cliente", "texto": mensaje[:100]})
        session.historial.append({"turno": session.turno, "rol": "sistema", "texto": r.respuesta_final})
        return None

    def _actualizar_session_emocion(self, r: ResultadoProcesamiento, session: SessionState):
        if r.emocion and r.emocion_fuente == "t":
            session.emocion_previa = r.emocion.emocion
            session.intensidad_previa = r.emocion.intensidad
