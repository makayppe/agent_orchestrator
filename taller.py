#!/usr/bin/env python3
"""CLI del demo Bintec: orquestador bancario con LLMs locales."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

load_dotenv()

console = Console()

MODELO_DECISION_DEFAULT = os.getenv("MODELO_DECISION", "qwen2.5:3b")
MODELO_REDACCION_DEFAULT = os.getenv("MODELO_REDACCION", "qwen2.5:7b")
CACHE_PATH = Path("cache.json")
MENSAJES_PATH = Path(__file__).parent / "data" / "mensajes.json"

COLORES = {
    "FRAUDE": "bold red",
    "REVERSOS": "bold yellow",
    "PQR": "bold blue",
    "FUERA_DE_ALCANCE": "dim",
    "desconocida": "dim",
}
COLORES_EMOCION = {
    "neutral": "dim",
    "confundido": "blue",
    "frustrado": "yellow",
    "enojado": "red",
    "ansioso": "magenta",
    "desconocida": "dim",
}


# ─── Helpers display ──────────────────────────────────────────────────────────

def _color_agente(agente: str) -> str:
    return COLORES.get(agente, "white")


def _icono_agente(agente: str) -> str:
    return {"FRAUDE": "🚨", "REVERSOS": "↩", "PQR": "📋", "FUERA_DE_ALCANCE": "⊘"}.get(agente, "•")


def _tag_emocion(emocion, fuente: str) -> str:
    if emocion is None:
        return ""
    nombre = emocion.emocion
    intens = emocion.intensidad
    color = COLORES_EMOCION.get(nombre, "white")
    fuente_txt = f" [dim]({fuente})[/]" if fuente != "t" else ""
    return f"[{color}]{nombre} / {intens}[/]{fuente_txt}"


def mostrar_traza(r, mensaje: str, args, sesion_num: int = 0):
    console.print()

    # Header with session number for multi-turn
    titulo_extra = f" [dim]· turno {sesion_num}[/]" if sesion_num > 0 else ""
    emocion_txt = _tag_emocion(r.emocion, r.emocion_fuente) if hasattr(r, "emocion") else ""

    console.print(Panel(
        f"[bold white]{mensaje}[/]\n{emocion_txt}" if emocion_txt else f"[bold white]{mensaje}[/]",
        title=f"[cyan]MENSAJE DEL CLIENTE{titulo_extra}[/]",
        border_style="cyan",
        padding=(0, 2),
    ))

    # Special flags
    if hasattr(r, "manipulacion_detectada") and r.manipulacion_detectada:
        console.print(Panel(
            "[bold red]MANIPULACIÓN DETECTADA — bloqueo inmediato[/]\n"
            "Se detectaron instrucciones para eludir reglas del sistema.",
            border_style="red", padding=(0, 2),
        ))
        _mostrar_reglas(r)
        _mostrar_respuesta(r)
        return

    # Saludo → solo la respuesta cordial (no hay acción bancaria)
    if getattr(r, "saludo", False):
        console.print(Panel(
            f"[green]{r.respuesta_final}[/]",
            title="[cyan]SALUDO[/]", border_style="cyan", padding=(0, 2),
        ))
        return

    # Identificación requerida (modo chat, cliente aún no verificado)
    if getattr(r, "requiere_identificacion", False):
        console.print(Panel(
            f"[bold blue]IDENTIFICACIÓN REQUERIDA[/]\n{r.respuesta_final}",
            border_style="blue", padding=(0, 2),
        ))
        return

    if getattr(r, "identificacion_ok", False):
        console.print("  [green]✓ Cliente identificado[/]")
        # Si solo fue saludo (sin solicitud pendiente), mostrar respuesta y salir
        if not r.decisiones and not r.resultados_agentes:
            _mostrar_respuesta(r)
            return

    if hasattr(r, "es_seguimiento") and r.es_seguimiento:
        console.print("  [dim]↩ Pregunta de seguimiento — usando estado guardado de sesión[/]")

    # ── Paso 1: Decisiones ────────────────────────────────────────────────────
    if r.decisiones:
        console.print(Rule("[bold cyan]PASO 1 · DECISIONES LLM[/]", style="cyan"))

        tabla = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", expand=True)
        tabla.add_column("Corrida", justify="center", width=7)
        tabla.add_column("Agente", justify="center", width=18)
        tabla.add_column("Fraude", justify="center", width=7)
        tabla.add_column("Urgencia", justify="center", width=9)
        tabla.add_column("Monto", justify="right", width=14)
        tabla.add_column("Razón", ratio=1)
        tabla.add_column("Latencia", justify="right", width=10)

        total_tokens_prompt = 0
        total_tokens_resp = 0
        total_lat_decision = 0.0

        for i, (d, m) in enumerate(zip(r.decisiones, r.metricas_decision), 1):
            color = _color_agente(d.agente)
            fraude_txt = "[red]✓[/]" if d.indicio_fraude else "[dim]—[/]"
            urg_color = "red" if d.urgencia >= 4 else "yellow" if d.urgencia == 3 else "green"
            monto_txt = f"${d.monto:,.0f}" if d.monto else "—"
            lat = m.get("latencia_ms", 0)
            total_lat_decision += lat
            total_tokens_prompt += m.get("tokens_prompt", 0)
            total_tokens_resp += m.get("tokens_respuesta", 0)

            tabla.add_row(
                str(i),
                f"[{color}]{_icono_agente(d.agente)} {d.agente}[/]",
                fraude_txt,
                f"[{urg_color}]{d.urgencia}/5[/]",
                monto_txt,
                f"[dim]{d.razon}[/]",
                f"[dim]{lat:.0f}ms[/]",
            )

        console.print(tabla)

        color_ag = _color_agente(r.agente_ganador)
        acuerdo_pct = r.acuerdo * 100
        acuerdo_color = "green" if r.acuerdo >= args.umbral else "red"
        console.print(
            f"  Decisión: [{color_ag}]{_icono_agente(r.agente_ganador)} {r.agente_ganador}[/]  "
            f"Acuerdo: [{acuerdo_color}]{acuerdo_pct:.0f}%[/] "
            f"(umbral: {args.umbral:.0%})  Corridas: {args.corridas}",
        )
    else:
        total_tokens_prompt = total_tokens_resp = 0
        total_lat_decision = 0.0

    if args.ronda == 1:
        _mostrar_metricas(r, total_tokens_prompt, total_tokens_resp, total_lat_decision, 0, args)
        return

    # ── Paso 2: Reglas ────────────────────────────────────────────────────────
    _mostrar_reglas(r)

    if args.ronda == 2:
        _mostrar_metricas(r, total_tokens_prompt, total_tokens_resp, total_lat_decision, 0, args)
        return

    # ── Paso 3: Agentes ────────────────────────────────────────────────────────
    if r.resultados_agentes:
        console.print(Rule("[bold cyan]PASO 3 · EJECUCIÓN DE AGENTES[/]", style="cyan"))
        for resultado in r.resultados_agentes:
            nombre = resultado["agente"]
            color = _color_agente(nombre)
            console.print(f"\n  [{color}]{_icono_agente(nombre)} {nombre}[/]")
            for herr in resultado.get("herramientas", []):
                fn = herr.get("herramienta", "?")
                ok = herr.get("ok", False)
                icono_h = "[green]✓[/]" if ok else "[red]✗[/]"
                detalle = herr.get("detalle") or herr.get("error") or ""
                ticket = herr.get("ticket") or herr.get("caso_id") or herr.get("radicado") or ""
                ticket_txt = f" [[bold]{ticket}[/]]" if ticket else ""
                console.print(f"    {icono_h} {fn}(){ticket_txt}")
                if detalle:
                    console.print(f"       [dim]{detalle}[/]")
        console.print()

    # ── Paso 4: Respuesta ─────────────────────────────────────────────────────
    _mostrar_respuesta(r)

    lat_red = r.metricas_redaccion.get("latencia_ms", 0)
    tok_red_p = r.metricas_redaccion.get("tokens_prompt", 0)
    tok_red_r = r.metricas_redaccion.get("tokens_respuesta", 0)
    _mostrar_metricas(
        r, total_tokens_prompt + tok_red_p,
        total_tokens_resp + tok_red_r,
        total_lat_decision, lat_red, args,
    )


def _mostrar_reglas(r):
    console.print(Rule("[bold cyan]PASO 2 · REGLAS DE NEGOCIO[/]", style="cyan"))
    for reg in sorted(r.reglas_disparadas, key=lambda x: x["num"]):
        icono = "[green]✓[/]" if reg["disparada"] else "[dim]✗[/]"
        desc_color = "white" if reg["disparada"] else "dim"
        detalle = f" → [italic]{reg['detalle']}[/]" if reg.get("detalle") else ""
        console.print(f"  {icono} Regla {reg['num']}: [{desc_color}]{reg['desc']}[/]{detalle}")
    console.print()

    if r.escalado_humano:
        console.print(Panel(
            f"[bold yellow]ESCALADO A HUMANO[/]\n{r.razon_escalado}",
            border_style="yellow", padding=(0, 2),
        ))
    elif r.requiere_aclaracion:
        console.print(Panel(
            f"[bold blue]SE REQUIERE ACLARACIÓN[/]\n{r.respuesta_final}",
            border_style="blue", padding=(0, 2),
        ))
    elif hasattr(r, "agentes_a_ejecutar") and r.agentes_a_ejecutar:
        agentes_txt = ", ".join(
            f"[{_color_agente(a)}]{_icono_agente(a)} {a}[/]" for a in r.agentes_a_ejecutar
        )
        console.print(f"  Agentes a ejecutar: {agentes_txt}")


def _mostrar_respuesta(r):
    console.print(Rule("[bold cyan]RESPUESTA AL CLIENTE[/]", style="cyan"))
    guardia_txt = " [dim](guardia activada)[/]" if getattr(r, "guardia_activada", False) else ""
    escalado = getattr(r, "escalado_humano", False)
    manipulacion = getattr(r, "manipulacion_detectada", False)
    border = "red" if manipulacion else ("yellow" if escalado else "green")
    console.print(Panel(
        f"[white]{r.respuesta_final}[/]{guardia_txt}",
        border_style=border, padding=(0, 2),
    ))


def _mostrar_metricas(r, tok_p, tok_r, lat_dec, lat_red, args):
    console.print(Rule("[dim]MÉTRICAS[/]", style="dim"))
    tabla = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    tabla.add_column("k", style="dim", width=22)
    tabla.add_column("v", style="bold")

    modelo_dec = r.metricas_decision[0].get("modelo", "?") if r.metricas_decision else "?"
    modelo_red = getattr(args, "writer_model", None) or args.modelo_redaccion
    lat_total = lat_dec + lat_red

    tabla.add_row("Modelo decisión", modelo_dec)
    if args.ronda == 3:
        tabla.add_row("Modelo redacción", modelo_red)
    lat_por_corrida = f" ({lat_dec / args.corridas:.0f} ms/corrida)" if args.corridas > 1 else ""
    tabla.add_row("Latencia decisión", f"{lat_dec:.0f} ms{lat_por_corrida}")
    if lat_red:
        tabla.add_row("Latencia redacción", f"{lat_red:.0f} ms")
    tabla.add_row("Latencia total", f"[bold]{lat_total:.0f} ms[/]")
    if tok_p or tok_r:
        tabla.add_row("Tokens prompt", str(tok_p))
        tabla.add_row("Tokens respuesta", str(tok_r))
        tabla.add_row("Tokens total", f"[bold]{tok_p + tok_r}[/]")
    if r.metricas_decision and r.metricas_decision[0].get("error"):
        tabla.add_row("[red]Advertencia[/]", r.metricas_decision[0]["error"][:80])

    console.print(tabla)


def mostrar_estado_sesion(session):
    from rich.panel import Panel
    tabla = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    tabla.add_column("k", style="dim", width=24)
    tabla.add_column("v")

    tabla.add_row("Sesión ID", session.id)
    tabla.add_row("Turno", str(session.turno))
    if session.cliente_id:
        tabla.add_row("Cliente", f"{session.cliente_nombre} ({session.cliente_cuenta})")
    else:
        tabla.add_row("Cliente", "[yellow]no identificado[/]")
    if session.solicitud_pendiente:
        tabla.add_row("Solicitud pendiente", session.solicitud_pendiente[:50])
    tabla.add_row("Contactos (mismo caso)", str(session.contactos_mismo_caso))
    tabla.add_row("Escalado", "[red]SÍ[/]" if session.escalado else "[green]NO[/]")
    if session.razon_escalado:
        tabla.add_row("Razón escalado", session.razon_escalado[:60])
    tabla.add_row("Emoción previa", f"{session.emocion_previa} / {session.intensidad_previa}")
    tabla.add_row("Fallbacks emoción", str(session.fallbacks_emocion_consecutivos))
    if session.reverso_id:
        tabla.add_row("Reverso activo", session.reverso_id)
    if session.caso_id:
        tabla.add_row("Caso activo", session.caso_id)
    if session.bloqueo_id:
        tabla.add_row("Bloqueo activo", session.bloqueo_id)
    tabla.add_row("Turnos en historial", str(len(session.historial)))

    console.print(Panel(tabla, title="[cyan]ESTADO DE SESIÓN[/]", border_style="cyan", padding=(0, 1)))


# ─── Cache ────────────────────────────────────────────────────────────────────

def _hash(msg: str) -> str:
    return hashlib.md5(msg.encode()).hexdigest()


def guardar_cache(mensaje: str, resultado_dict: dict):
    cache = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    cache[_hash(mensaje)] = {"mensaje": mensaje, "resultado": resultado_dict}
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[dim]Guardado en caché: {CACHE_PATH}[/]")


def cargar_cache(mensaje: str) -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        entrada = cache.get(_hash(mensaje))
        return entrada["resultado"] if entrada else None
    except Exception:
        return None


# ─── Verificación Ollama ──────────────────────────────────────────────────────

def verificar_y_avisar(modelo_decision: str, modelo_redaccion: str, mock: bool) -> bool:
    if mock:
        console.print("[dim]Modo mock activo — se omite verificación de Ollama[/]")
        return True

    from llm import verificar_ollama
    estado = verificar_ollama(modelo_decision, modelo_redaccion)

    if estado["ok"]:
        modelos = ", ".join(estado["modelos_disponibles"])
        console.print(f"[green]✓ Ollama OK[/] — modelos disponibles: [dim]{modelos}[/]")
        return True

    console.print(Panel(
        "\n".join(f"[red]✗[/] {e}" for e in estado["errores"]),
        title="[red]ERROR: Ollama no disponible[/]",
        border_style="red",
    ))
    return False


# ─── Modos de ejecución ───────────────────────────────────────────────────────

def _construir_orquestador(args, requiere_identificacion: bool = False):
    from orquestador import Orquestador
    writer_model = getattr(args, "writer_model", None) or args.modelo_redaccion
    return Orquestador(
        modelo_decision=args.modelo_decision,
        modelo_redaccion=writer_model,
        mock=args.mock,
        corridas=args.corridas,
        umbral_acuerdo=args.umbral,
        umbral_monto=args.umbral_monto,
        temperatura=args.temperatura,
        ronda=args.ronda,
        cliente_id=args.cliente,
        emotion_timeout_ms=getattr(args, "emotion_timeout_ms", 1200),
        emotion_delay_ms=getattr(args, "emotion_delay_ms", 0),
        decision_timeout_ms=getattr(args, "decision_timeout_ms", 8000),
        requiere_identificacion=requiere_identificacion,
        audit_path="audit.jsonl" if getattr(args, "auditar", False) else None,
    )


def procesar_uno(mensaje: str, args, session=None, sesion_num: int = 0) -> None:
    from orquestador import ResultadoProcesamiento

    # Replay mode
    if args.replay:
        cached = cargar_cache(mensaje)
        if cached:
            console.print("[dim]Reproduciendo desde caché...[/]")
            r = ResultadoProcesamiento.desde_dict(cached)
            mostrar_traza(r, mensaje, args, sesion_num)
            return
        console.print("[yellow]No hay caché para este mensaje. Procesando en vivo...[/]")

    orch = _construir_orquestador(args)
    r = orch.procesar(mensaje, session)
    mostrar_traza(r, mensaje, args, sesion_num)

    if args.grabar:
        guardar_cache(mensaje, r.a_dict())


def modo_lote(args) -> None:
    from orquestador import Orquestador

    mensajes = json.loads(MENSAJES_PATH.read_text(encoding="utf-8"))
    orch = _construir_orquestador(args)

    tabla = Table(
        title=f"[bold]LOTE — {len(mensajes)} mensajes · modelo: {args.modelo_decision}[/]",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    tabla.add_column("#", width=3, justify="right")
    tabla.add_column("Mensaje", ratio=2)
    tabla.add_column("Esperado", width=16)
    tabla.add_column("Obtenido", width=16)
    tabla.add_column("OK", width=4, justify="center")
    tabla.add_column("Latencia", width=9, justify="right")
    tabla.add_column("Tokens", width=8, justify="right")
    tabla.add_column("Esc.", width=4, justify="center")

    correctos = 0
    escalados = 0
    total_lat = 0.0
    total_tok = 0

    for msg_data in mensajes:
        mid = msg_data["id"]
        mensaje = msg_data["mensaje"]
        etiqueta = msg_data["etiqueta"]

        r = orch.procesar(mensaje)

        lat = sum(m.get("latencia_ms", 0) for m in r.metricas_decision)
        lat += r.metricas_redaccion.get("latencia_ms", 0)
        tok = sum(
            m.get("tokens_prompt", 0) + m.get("tokens_respuesta", 0)
            for m in r.metricas_decision
        )
        tok += r.metricas_redaccion.get("tokens_prompt", 0) + r.metricas_redaccion.get("tokens_respuesta", 0)

        manipulacion = getattr(r, "manipulacion_detectada", False)
        ok = (r.agente_ganador == etiqueta
              or (manipulacion and etiqueta == "FUERA_DE_ALCANCE")
              or (etiqueta == "REVERSOS" and "REVERSOS" in r.agentes_a_ejecutar)
              or (etiqueta == "FRAUDE" and "FRAUDE" in r.agentes_a_ejecutar))
        if ok:
            correctos += 1
        if r.escalado_humano:
            escalados += 1
        total_lat += lat
        total_tok += tok

        color_esp = _color_agente(etiqueta)
        color_obt = _color_agente(r.agente_ganador)
        ok_txt = "[green]✓[/]" if ok else "[red]✗[/]"
        esc_txt = "[yellow]⬆[/]" if r.escalado_humano else "[dim]—[/]"

        tabla.add_row(
            str(mid),
            mensaje[:55] + ("…" if len(mensaje) > 55 else ""),
            f"[{color_esp}]{etiqueta}[/]",
            f"[{color_obt}]{r.agente_ganador}[/]",
            ok_txt,
            f"{lat:.0f}ms",
            str(tok) if tok else "—",
            esc_txt,
        )

    console.print(tabla)
    pct = correctos / len(mensajes) * 100
    color_pct = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
    console.print(
        f"\n  Acierto: [{color_pct}]{correctos}/{len(mensajes)} ({pct:.0f}%)[/]  "
        f"Escalados: {escalados}  "
        f"Latencia media: {total_lat/len(mensajes):.0f}ms  "
        f"Tokens totales: {total_tok}"
    )


def modo_barrido(args) -> None:
    """Barrido de umbrales: mide acierto vs. tasa de escalado sobre el lote.

    Respalda el mensaje 'el equilibrio automatizar/escalar es medible': al mover
    el umbral de monto se ve cómo cambian aciertos y escalamientos.
    """
    from orquestador import Orquestador

    mensajes = json.loads(MENSAJES_PATH.read_text(encoding="utf-8"))

    # Umbrales de monto a barrer (en millones para legibilidad)
    umbrales = [500_000, 1_000_000, 2_000_000, 3_000_000, 5_000_000]

    tabla = Table(
        title=f"[bold]BARRIDO DE UMBRAL DE MONTO — {len(mensajes)} mensajes · {args.modelo_decision}[/]",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    tabla.add_column("Umbral monto", justify="right", width=14)
    tabla.add_column("Acierto", justify="center", width=12)
    tabla.add_column("Escalados", justify="center", width=11)
    tabla.add_column("Tasa escalado", justify="right", width=14)

    for umbral in umbrales:
        args.umbral_monto = umbral
        orch = _construir_orquestador(args)
        correctos = 0
        escalados = 0
        for msg_data in mensajes:
            r = orch.procesar(msg_data["mensaje"])
            etiqueta = msg_data["etiqueta"]
            manipulacion = getattr(r, "manipulacion_detectada", False)
            ok = (r.agente_ganador == etiqueta
                  or (manipulacion and etiqueta == "FUERA_DE_ALCANCE")
                  or (etiqueta == "REVERSOS" and "REVERSOS" in r.agentes_a_ejecutar)
                  or (etiqueta == "FRAUDE" and "FRAUDE" in r.agentes_a_ejecutar))
            if ok:
                correctos += 1
            if r.escalado_humano:
                escalados += 1

        n = len(mensajes)
        pct_ok = correctos / n * 100
        pct_esc = escalados / n * 100
        color_ok = "green" if pct_ok >= 80 else "yellow" if pct_ok >= 60 else "red"
        tabla.add_row(
            f"${umbral:,.0f}",
            f"[{color_ok}]{correctos}/{n} ({pct_ok:.0f}%)[/]",
            str(escalados),
            f"{pct_esc:.0f}%",
        )

    console.print(tabla)
    console.print(
        "\n  [dim]Umbral bajo → más escalamientos (más seguro, menos automatizado). "
        "Umbral alto → menos escalamientos (más automatizado, más riesgo).[/]"
    )


def modo_chat(args) -> None:
    """Multi-turn chat loop — maintains session state across turns."""
    from orquestador import Orquestador, SessionState

    console.print(Panel(
        "[bold cyan]Modo chat[/] — conversación multi-turno.\n"
        "El bot pedirá identificación (cédula o últimos 4 de la cuenta) antes de atender.\n"
        "Comandos: [bold]/salir[/] · [bold]/reset[/] (nueva sesión) · [bold]/estado[/] (ver sesión) · [bold]/modelo <nombre>[/]",
        border_style="cyan",
    ))

    session = SessionState()
    orch = _construir_orquestador(args, requiere_identificacion=True)

    while True:
        try:
            console.print()
            entrada = console.input("[bold cyan]> Mensaje:[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Saliendo...[/]")
            break

        if not entrada:
            continue

        cmd = entrada.lower()

        if cmd == "/salir":
            break

        if cmd == "/reset":
            session = SessionState()
            console.print("[yellow]Sesión reiniciada.[/]")
            continue

        if cmd == "/estado":
            mostrar_estado_sesion(session)
            continue

        if cmd.startswith("/modelo "):
            args.modelo_decision = cmd.split(" ", 1)[1].strip()
            orch = _construir_orquestador(args, requiere_identificacion=True)
            console.print(f"[yellow]Modelo de decisión cambiado a: {args.modelo_decision}[/]")
            continue

        if cmd == "/lote":
            modo_lote(args)
            continue

        if cmd == "/mock":
            args.mock = not args.mock
            orch = _construir_orquestador(args, requiere_identificacion=True)
            console.print(f"[yellow]Modo mock: {'ON' if args.mock else 'OFF'}[/]")
            continue

        r = orch.procesar(entrada, session)
        mostrar_traza(r, entrada, args, sesion_num=session.turno)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Demo taller Bintec — orquestador bancario con LLMs locales",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python taller.py "Pagué con QR y me cobraron dos veces"
  python taller.py "mensaje" --ronda 1
  python taller.py "mensaje" --corridas 3 --umbral 0.67
  python taller.py --chat --mock
  python taller.py --lote --modelo qwen2.5:3b
  python taller.py "mensaje" --mock --emotion-delay-ms 2000
""",
    )

    parser.add_argument("mensaje", nargs="?", help="Mensaje del cliente a procesar")
    parser.add_argument("--ronda", type=int, choices=[1, 2, 3], default=3,
                        help="1: solo enrutar · 2: +reglas · 3: +agentes y respuesta (default: 3)")
    parser.add_argument("--corridas", type=int, default=1,
                        help="Número de decisiones para autoconsistencia (default: 1)")
    parser.add_argument("--umbral", type=float, default=0.67,
                        help="Umbral de acuerdo para autoconsistencia (default: 0.67)")
    parser.add_argument("--umbral-monto", dest="umbral_monto", type=float, default=2_000_000,
                        help="Monto para escalar a humano (default: 2000000)")
    parser.add_argument("--temperatura", type=float, default=0.4,
                        help="Temperatura del modelo de decisión (default: 0.4)")
    parser.add_argument("--modelo-decision", dest="modelo_decision",
                        default=MODELO_DECISION_DEFAULT,
                        help=f"Modelo Ollama para decisión (default: {MODELO_DECISION_DEFAULT})")
    parser.add_argument("--modelo-redaccion", dest="modelo_redaccion",
                        default=MODELO_REDACCION_DEFAULT,
                        help=f"Modelo Ollama para redacción (default: {MODELO_REDACCION_DEFAULT})")
    parser.add_argument("--modelo", dest="modelo_decision",
                        help="Alias rápido para --modelo-decision (demo en vivo)")
    parser.add_argument("--writer-model", dest="writer_model", default=None,
                        help="Alias para --modelo-redaccion (demo en vivo)")
    parser.add_argument("--emotion-delay-ms", dest="emotion_delay_ms", type=int, default=0,
                        help="Forzar delay en clasificación de emoción (para demo de timeout)")
    parser.add_argument("--emotion-timeout-ms", dest="emotion_timeout_ms", type=int, default=1200,
                        help="Timeout para clasificación de emoción en ms (default: 1200)")
    parser.add_argument("--decision-timeout-ms", dest="decision_timeout_ms", type=int, default=8000,
                        help="Timeout para clasificación de decisión en ms (default: 8000)")
    parser.add_argument("--cliente", default="C001",
                        help="ID del cliente para las herramientas (default: C001)")
    parser.add_argument("--chat", action="store_true",
                        help="Modo chat: conversación multi-turno con estado de sesión")
    parser.add_argument("--interactivo", action="store_true",
                        help="Alias de --chat (compatibilidad)")
    parser.add_argument("--lote", action="store_true",
                        help="Procesar todos los mensajes de data/mensajes.json")
    parser.add_argument("--barrido", action="store_true",
                        help="Barrido de umbral de monto: acierto vs. tasa de escalado")
    parser.add_argument("--auditar", action="store_true",
                        help="Registrar cada turno en audit.jsonl")
    parser.add_argument("--mock", action="store_true",
                        help="Modo mock: sin LLM, respuestas fijas (plan B)")
    parser.add_argument("--grabar", action="store_true",
                        help="Guardar respuestas en cache.json")
    parser.add_argument("--replay", action="store_true",
                        help="Reproducir respuestas desde cache.json si existen")

    args = parser.parse_args()

    writer_model = args.writer_model or args.modelo_redaccion

    console.print(Panel(
        "[bold cyan]Demo Bintec[/] — Orquestador bancario con LLMs locales\n"
        f"[dim]Decisión: [white]{args.modelo_decision}[/]  "
        f"Redacción: [white]{writer_model}[/]  "
        f"Ronda: {args.ronda}  Corridas: {args.corridas}  "
        f"Mock: {'[yellow]ON[/]' if args.mock else 'OFF'}[/]",
        border_style="cyan", padding=(0, 2),
    ))

    solo_replay = args.replay and not args.grabar and not args.lote and not (args.chat or args.interactivo)
    if not verificar_y_avisar(args.modelo_decision, writer_model, args.mock or solo_replay):
        console.print("[yellow]Tip: usa --mock para correr sin Ollama.[/]")
        sys.exit(1)

    if args.chat or args.interactivo:
        modo_chat(args)
    elif args.barrido:
        modo_barrido(args)
    elif args.lote:
        modo_lote(args)
    elif args.mensaje:
        procesar_uno(args.mensaje, args)
    else:
        # No message → open chat mode by default
        modo_chat(args)


if __name__ == "__main__":
    main()
