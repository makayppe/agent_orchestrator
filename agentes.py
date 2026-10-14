"""Los tres agentes bancarios con herramientas simuladas sobre datos.json."""

from __future__ import annotations

import uuid

import datastore


def identificar_cliente(texto: str) -> dict | None:
    """Busca un cliente por cédula o por los últimos 4 dígitos de la cuenta."""
    return datastore.buscar_cliente_por_identificador(texto)


# ─── Fraude ──────────────────────────────────────────────────────────────────

class AgenteFraude:
    nombre = "FRAUDE"

    def bloquear_medio(self, cliente_id: str, medio: str = "QR") -> dict:
        cliente = datastore.cliente(cliente_id)
        if not cliente:
            return {"ok": False, "herramienta": "bloquear_medio", "error": "Cliente no encontrado"}
        ticket = f"BLQ-{uuid.uuid4().hex[:8].upper()}"
        return {
            "ok": True,
            "herramienta": "bloquear_medio",
            "ticket": ticket,
            "cliente": cliente["nombre"],
            "medio_bloqueado": medio,
            "detalle": f"Medio '{medio}' bloqueado preventivamente para {cliente['nombre']}",
        }

    def abrir_caso(self, cliente_id: str, descripcion: str, monto: float = 0.0) -> dict:
        caso = f"CASO-{uuid.uuid4().hex[:8].upper()}"
        prioridad = "CRÍTICA" if monto > 500000 else "ALTA" if monto > 100000 else "MEDIA"
        return {
            "ok": True,
            "herramienta": "abrir_caso",
            "caso_id": caso,
            "cliente_id": cliente_id,
            "descripcion": descripcion[:80],
            "monto_afectado": monto,
            "prioridad": prioridad,
            "estado": "ABIERTO",
            "detalle": f"Caso de fraude {caso} abierto con prioridad {prioridad}",
        }

    def ejecutar(self, mensaje: str, cliente_id: str = "C001", session=None) -> dict:
        txs = datastore.transacciones_cliente(cliente_id)
        monto = sum(
            t["monto"] for t in txs
            if "sospechosa" in t.get("nota", "").lower()
            or "doble cobro" in t.get("nota", "").lower()
        )

        # Idempotency: reuse existing bloqueo_id if in session
        if session and session.bloqueo_id:
            r_bloqueo = {
                "ok": True, "herramienta": "bloquear_medio",
                "ticket": session.bloqueo_id, "medio_bloqueado": "QR",
                "detalle": f"Bloqueo previo reutilizado ({session.bloqueo_id})",
            }
        else:
            r_bloqueo = self.bloquear_medio(cliente_id, "QR")
            if session:
                session.bloqueo_id = r_bloqueo["ticket"]

        if session and session.caso_id:
            r_caso = {
                "ok": True, "herramienta": "abrir_caso",
                "caso_id": session.caso_id, "prioridad": "ALTA", "estado": "ABIERTO",
                "detalle": f"Caso previo reutilizado ({session.caso_id})",
            }
        else:
            r_caso = self.abrir_caso(cliente_id, f"Posible fraude: {mensaje[:60]}", monto)
            if session:
                session.caso_id = r_caso["caso_id"]

        return {
            "agente": self.nombre,
            "herramientas": [r_bloqueo, r_caso],
            "resumen": (
                f"Caso {r_caso['caso_id']} abierto (prioridad {r_caso['prioridad']}), "
                f"medio QR bloqueado ({r_bloqueo['ticket']})"
            ),
        }


# ─── Reversos ────────────────────────────────────────────────────────────────

class AgenteReversos:
    nombre = "REVERSOS"
    # Tope para reverso automático sin confirmación humana. Por encima de esto,
    # o si no hay un doble cobro claro, se pide confirmación / se escala.
    TOPE_REVERSO_AUTOMATICO = 1_000_000

    def consultar_transacciones(self, cliente_id: str, limite: int = 5) -> dict:
        txs = sorted(datastore.transacciones_cliente(cliente_id), key=lambda x: x["fecha"], reverse=True)
        return {
            "ok": True,
            "herramienta": "consultar_transacciones",
            "transacciones": txs[:limite],
            "total_en_cuenta": len(txs),
            "detalle": f"Se encontraron {len(txs)} transacciones, mostrando las {min(limite, len(txs))} más recientes",
        }

    def solicitar_reverso(self, transaccion_id: str, motivo: str) -> dict:
        tx = datastore.transaccion(transaccion_id)
        if not tx:
            return {
                "ok": False,
                "herramienta": "solicitar_reverso",
                "error": f"Transacción {transaccion_id} no encontrada",
            }
        ticket = f"REV-{uuid.uuid4().hex[:8].upper()}"
        return {
            "ok": True,
            "herramienta": "solicitar_reverso",
            "ticket": ticket,
            "transaccion_id": transaccion_id,
            "monto": tx["monto"],
            "comercio": tx["comercio"],
            "motivo": motivo,
            "estado": "EN_PROCESO",
            "tiempo_estimado": "3-5 días hábiles",
            "detalle": (
                f"Reverso {ticket} iniciado: ${tx['monto']:,.0f} de {tx['comercio']} "
                f"— tiempo estimado: 3-5 días hábiles"
            ),
        }

    def ejecutar(self, mensaje: str, cliente_id: str = "C001", session=None) -> dict:
        r_txs = self.consultar_transacciones(cliente_id)
        txs = r_txs["transacciones"]

        # Solo se reversa automáticamente ante un DOBLE COBRO claro
        # (mismo comercio + mismo monto). Si no lo hay, NO se reversa la
        # transacción más reciente a ciegas: se pide confirmación.
        tx_revertir = None
        for i, tx in enumerate(txs):
            for tx2 in txs[i + 1:]:
                if tx["monto"] == tx2["monto"] and tx["comercio"] == tx2["comercio"]:
                    tx_revertir = tx2
                    break
            if tx_revertir:
                break

        herramientas = [r_txs]

        # Caso 1: no hay doble cobro claro → pedir confirmación, sin reversar.
        if not tx_revertir:
            return {
                "agente": self.nombre,
                "herramientas": herramientas,
                "requiere_confirmacion": True,
                "resumen": (
                    f"Revisadas {len(txs)} transacciones. No se identificó un doble cobro claro; "
                    "se requiere que el cliente confirme cuál transacción desea reversar."
                ),
            }

        # Caso 2: doble cobro por encima del tope → confirmación humana, sin reversar.
        if tx_revertir["monto"] > self.TOPE_REVERSO_AUTOMATICO:
            return {
                "agente": self.nombre,
                "herramientas": herramientas,
                "requiere_confirmacion": True,
                "resumen": (
                    f"Doble cobro detectado por ${tx_revertir['monto']:,.0f} en {tx_revertir['comercio']}, "
                    f"por encima del tope de reverso automático (${self.TOPE_REVERSO_AUTOMATICO:,.0f}). "
                    "Requiere confirmación humana antes de proceder."
                ),
            }

        # Caso 3: doble cobro claro y por debajo del tope → reverso automático.
        # Idempotency: same monto+comercio in same session → reuse existing reverso ticket
        if (session and session.reverso_id
                and session.reverso_monto == tx_revertir["monto"]
                and session.reverso_comercio == tx_revertir["comercio"]):
            r_rev = {
                "ok": True, "herramienta": "solicitar_reverso",
                "ticket": session.reverso_id, "monto": tx_revertir["monto"],
                "comercio": tx_revertir["comercio"], "motivo": "Idempotente — reverso previo",
                "estado": "EN_PROCESO", "tiempo_estimado": "3-5 días hábiles",
                "detalle": f"Reverso previo reutilizado: {session.reverso_id}",
            }
        else:
            r_rev = self.solicitar_reverso(tx_revertir["id"], "Doble cobro detectado")
            if session and r_rev.get("ok"):
                session.reverso_id = r_rev["ticket"]
                session.reverso_monto = tx_revertir["monto"]
                session.reverso_comercio = tx_revertir["comercio"]
        herramientas.append(r_rev)

        resumen = f"Revisadas {len(txs)} transacciones."
        if r_rev.get("ok"):
            resumen += f" Reverso {r_rev['ticket']} iniciado por ${r_rev['monto']:,.0f}."

        return {
            "agente": self.nombre,
            "herramientas": herramientas,
            "resumen": resumen,
        }


# ─── PQR ─────────────────────────────────────────────────────────────────────

class AgentePQR:
    nombre = "PQR"

    def radicar_pqr(self, cliente_id: str, tipo: str, descripcion: str) -> dict:
        radicado = f"PQR-{uuid.uuid4().hex[:8].upper()}"
        tiempos = {"QUEJA": "15 días hábiles", "PETICION": "15 días hábiles", "RECLAMO": "15 días hábiles"}
        return {
            "ok": True,
            "herramienta": "radicar_pqr",
            "radicado": radicado,
            "cliente_id": cliente_id,
            "tipo": tipo,
            "descripcion": descripcion[:100],
            "estado": "RADICADO",
            "tiempo_respuesta": tiempos.get(tipo, "15 días hábiles"),
            "detalle": f"PQR {radicado} radicada. Tiempo de respuesta: {tiempos.get(tipo, '15 días hábiles')}",
        }

    def ejecutar(self, mensaje: str, cliente_id: str = "C001", session=None) -> dict:
        msg = mensaje.lower()
        if any(w in msg for w in ["queja", "mal servicio", "inconveniente"]):
            tipo = "QUEJA"
        elif any(w in msg for w in ["reclamo", "cobro", "cargo", "devolver"]):
            tipo = "RECLAMO"
        else:
            tipo = "PETICION"

        r = self.radicar_pqr(cliente_id, tipo, mensaje[:100])
        return {
            "agente": self.nombre,
            "herramientas": [r],
            "resumen": f"PQR {tipo} radicada con número {r['radicado']}. Respuesta en {r['tiempo_respuesta']}.",
        }


# ─── Registro ─────────────────────────────────────────────────────────────────

AGENTES: dict[str, AgenteFraude | AgenteReversos | AgentePQR] = {
    "FRAUDE": AgenteFraude(),
    "REVERSOS": AgenteReversos(),
    "PQR": AgentePQR(),
}
