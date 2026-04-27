# Solución – Notification Service Challenge

## Qué hace la aplicación

Servicio FastAPI que actúa como mediador entre los clientes y un provider externo de notificaciones. El cliente registra la solicitud, dispara su procesamiento y consulta el estado. La app se encarga de hablar con el provider absorbiendo su latencia, sus rate limits y sus fallos aleatorios.

---

## Arquitectura del flujo

```
POST /v1/requests              → guarda en memoria, devuelve id            (201)
POST /v1/requests/{id}/process → encola en una asyncio.Queue, responde     (202)
GET  /v1/requests/{id}         → devuelve estado actual                     (200)

Worker pool (corutinas en background):
  1. Lee un id de la cola
  2. Marca el estado como "processing"
  3. Llama al provider (con retry sobre 429 y 5XX)
  4. Actualiza el estado: sent | failed
```

---

## Decisiones de diseño

### 1. Procesamiento asíncrono (cola + worker pool, `202 Accepted`)

El provider tiene una latencia de 100–500 ms por petición y solo tolera 50 conexiones concurrentes. Si el endpoint `/process` esperara la respuesta de forma síncrona, bajo 200 usuarios concurrentes los timeouts se dispararían y el throughput colapsaría.

La solución: devolver `202` inmediatamente y meter el id en una `asyncio.Queue`. Un pool fijo de workers lee de la cola y habla con el provider. El cliente puede consultar el estado cuando quiera mediante `GET /v1/requests/{id}`.

### 2. Worker pool en lugar de `BackgroundTasks`

`BackgroundTasks` de FastAPI lanza una corutina por petición sin límite. Con 200 VUs eso son 200 llamadas simultáneas al provider, que tiene un rate limit de 50 req/10 s y un máximo de 50 conexiones concurrentes: nos comeríamos los `429` por nuestra propia culpa.

Un pool de **30 workers** consumiendo de la cola limita la concurrencia aguas abajo. El número está por debajo del techo de 50 del provider y deja margen para que los reintentos no empujen al límite.

### 3. Cliente HTTP singleton con pool de conexiones

Se crea un único `httpx.AsyncClient` al arrancar la app (patrón `lifespan` de FastAPI) con `max_connections=100` y `max_keepalive_connections=50`. Reutiliza conexiones TCP keep-alive entre requests. Crear un cliente nuevo por cada petición sería mucho menos eficiente.

### 4. Almacén en memoria (`dict`)

No hay requisito de persistencia y los tests duran menos de un minuto. Meter Redis o SQLite sería demasiado para el caso de uso. Las operaciones `get`/`set` por clave en un `dict` de CPython son atómicas dentro de una corutina (no hay yields entre ellas), así que no hace falta `Lock` para este flujo.

En producción real iría a Redis o Postgres para sobrevivir a reinicios y permitir varias réplicas, pero eso queda fuera del alcance.

### 5. Reintentos con `tenacity` sobre errores transitorios

El provider puede devolver:
- `429` cuando se sobrepasa el rate limit (50 req cada 10 s).
- `500` con un 10% de probabilidad aleatoria.
- Latencias que pueden disparar timeouts puntuales.

Se usa `tenacity` (incluido en `requirements.txt`) con 3 intentos y espera exponencial (0,2 → 2 s máx). Solo se reintenta en errores transitorios (`429`, `5XX` y errores de red); los `4XX` no transitorios (401, 400) son deterministas y no merece la pena reintentarlos.

### 6. Estados de la solicitud

La máquina de estados es la que pide el contrato:

```
queued → processing → sent
                  ↘ failed
```

Cada transición es un `SET` directo en el diccionario. El `GET` siempre devuelve uno de los cuatro literales válidos.

### 7. Validación con Pydantic

El modelo `NotificationIn` valida en la entrada que:
- `to` no esté vacío.
- `message` no esté vacío.
- `type` sea `email`, `sms` o `push`.

Si el cuerpo no cumple, FastAPI devuelve `422` automáticamente y nunca se llega a guardar nada. Eso evita basura en el almacenamiento y peticiones inútiles al provider.

### 8. Detalle de red: `network_mode: "service:provider"`

En `docker-compose.yaml` el servicio `app` comparte el namespace de red con `provider`. Por eso el provider se llama desde la app como `http://localhost:3001`, no como `http://provider:3001`. Es un detalle pequeño pero si se cambia, no resuelve.

---

## Estructura del código (`app/main.py`)

```
Constantes de configuración
  PROVIDER_URL, API_KEY, WORKERS, HTTP_TIMEOUT, MAX_RETRIES

Modelos Pydantic
  NotificationIn   → entrada del POST /v1/requests
  CreatedOut       → respuesta del POST /v1/requests
  StatusOut        → respuesta del GET /v1/requests/{id}

Estado en memoria
  requests_store   → dict[id → {status, payload}]
  queue            → asyncio.Queue[str] de ids pendientes

Provider
  RetryableProviderError  → marcador de errores transitorios
  send_to_provider()      → llama al provider con retry (tenacity)

Worker
  worker()         → bucle infinito: saca id, procesa, actualiza estado

Lifespan
  lifespan()       → crea httpx.AsyncClient, arranca workers, los cancela al cerrar

Endpoints
  POST /v1/requests
  POST /v1/requests/{id}/process
  GET  /v1/requests/{id}
```

---

## Cómo ejecutar

```bash
# 1. Infraestructura
docker-compose up -d provider influxdb grafana

# 2. Aplicación
docker-compose up -d --build app

# 3. Test de carga (k6)
docker-compose run --rm load-test

# 4. Resultados en Grafana
# http://localhost:3000/d/backend-performance-scorecard/
```

### Pruebas

Se ha añadido al repositorio una colección de Postman para importarla y probar el flujo completo desde ahí: crear, procesar, consultar estado y casos negativos (404 y validación 422).
