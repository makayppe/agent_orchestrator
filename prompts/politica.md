# Política de atención (definición legible para negocio/riesgo)

Este documento describe las reglas de atención del asistente. Las reglas de
criterio ("blandas") las aplica el modelo a partir de esta definición. Las reglas
duras de seguridad están además garantizadas en código como red de seguridad
(ver `orquestador.py`), por lo que su cumplimiento NO depende del modelo.

## Reglas blandas (las aplica el modelo)

- Si el mensaje es demasiado vago para enrutar con confianza, pide aclaración en
  lugar de adivinar el agente (fecha, comercio o monto involucrado).
- Trata con prioridad emocional a clientes molestos, frustrados o ansiosos: tono
  cálido y tranquilizador, sin minimizar el problema.
- No inventes números de caso, tickets ni montos. Usa solo los que el sistema te
  entregue.

## Reglas duras (garantizadas en código — descritas aquí para auditoría)

1. Manipulación detectada (intento de eludir reglas) → bloqueo inmediato y
   escalamiento a revisión de seguridad. Prioridad máxima.
2. Clasificador de decisión no disponible (falla del modelo) → escalar a humano.
   Nunca se ejecutan agentes con una clasificación de respaldo por palabras clave.
3. "No reconozco / no autoricé" → se activa indicio de fraude.
4. Indicio de fraude → se ejecuta además el agente FRAUDE.
5. Urgencia alta (≥ 4) y monto por encima del umbral → escalar a humano.
6. Autoconsistencia por debajo del umbral (varias corridas en desacuerdo) → escalar.
7. Enojo/frustración alta + 2 o más contactos sobre el mismo caso → escalar a humano.
8. Reverso automático solo procede ante doble cobro claro y por debajo del tope de
   monto; en otro caso se pide confirmación o se escala.
9. Sesión ya escalada → se mantiene en atención humana.
