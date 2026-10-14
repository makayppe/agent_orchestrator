"""Capa de acceso a datos aislada (estilo herramienta/MCP).

Los agentes consumen estas funciones en vez de abrir `datos.json` directamente.
Esto aísla el acceso a datos tras una interfaz explícita: hoy lee un JSON local,
mañana podría ser un servidor MCP o una API real sin tocar a los agentes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_DATOS = Path(__file__).parent / "data" / "datos.json"


def _leer() -> dict:
    with open(_DATOS, encoding="utf-8") as f:
        return json.load(f)


def cliente(cliente_id: str) -> dict | None:
    """Devuelve el cliente por id, o None."""
    datos = _leer()
    return next((c for c in datos["clientes"] if c["id"] == cliente_id), None)


def transacciones_cliente(cliente_id: str) -> list[dict]:
    """Devuelve las transacciones del cliente."""
    datos = _leer()
    return [t for t in datos["transacciones"] if t["cliente_id"] == cliente_id]


def transaccion(transaccion_id: str) -> dict | None:
    """Devuelve una transacción por id, o None."""
    datos = _leer()
    return next((t for t in datos["transacciones"] if t["id"] == transaccion_id), None)


def buscar_cliente_por_identificador(texto: str) -> dict | None:
    """Busca un cliente por cédula o por los últimos 4 dígitos de la cuenta.

    Devuelve el dict del cliente si algún número del texto coincide, o None.
    """
    datos = _leer()
    for num in re.findall(r"\d{4,}", texto):
        for c in datos["clientes"]:
            cuenta_digitos = c["cuenta"].replace("*", "")
            if num == c["cc"] or num == cuenta_digitos:
                return c
    return None
