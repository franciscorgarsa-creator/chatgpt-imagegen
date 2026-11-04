# ImageGen para IA Design

La API incluida en este repositorio sirve imágenes binarias a `sistema-imprenta`.
Funciona tanto en Docker como con `python -m uvicorn api:app`; encuentra el CLI
junto a `api.py`, usando el mismo intérprete Python que el servidor.

## Aplicación en Dokploy

No hace falta convertir la app a Docker Compose. Mantén el despliegue por
Dockerfile de este repositorio:

1. Configura `IMAGEGEN_API_TOKEN` como variable privada de la app y usa el mismo
   valor para `IMAGEGEN_TOKEN` en la app del ERP. Configura
   `CHATGPT_IMAGEGEN_BACKEND=codex`.
   Si la cuenta rechaza el modelo predeterminado `gpt-5.5`, configura también
   `CHATGPT_IMAGEGEN_MODEL=gpt-5.4` (o un modelo disponible para esa cuenta).
   El CLI ya respeta esta variable, incluso con la versión original de la API.
2. Añade volúmenes persistentes con destino `/root/.codex` y `/data`. El primero
   conserva el inicio de sesión y su renovación; el segundo, la configuración.
   La declaración VOLUME del Dockerfile no sustituye estos mounts persistentes
   entre despliegues de Dokploy.
3. En el dominio de la app, dirige el tráfico al **puerto interno 8000**. Usa en
   el ERP `IMAGEGEN_URL=https://<dominio-de-la-app-imagegen>`, sin `/generate`.
   Si mantienes el prefijo `/iadesign-base`, el proxy debe quitarlo antes de
   enviar la petición a Uvicorn. `GET /health` debe devolver JSON del servicio,
   no una página HTML del ERP ni un error 404.
4. Despliega los cambios y, en la terminal del **contenedor de ImageGen**, ejecuta
   `codex login --device-auth`. Completa el inicio de sesión del proveedor con
   la cuenta que utilizará el servicio. No ejecutes ese comando en el contenedor
   del ERP. Si habías configurado `IMAGEGEN_CODEX_AUTH_DIR`, ese directorio debe
   contener la sesión válida; elimínalo de la configuración si vas a utilizar
   la sesión predeterminada guardada en `/root/.codex`.
5. Conserva las variables Cloudinary en el ERP y vuelve a desplegar también esa
   app con el código de integración actualizado. Prueba una generación real
   desde IA Design y comprueba que aparece en el historial.

Si Dokploy o un proxy anterior impone un límite de tiempo, permite solicitudes
de al menos 500 segundos. Cuando la sesión caduque sin poder renovarse, repite el
inicio de sesión en ese mismo contenedor; no cambies el token compartido entre
servicios para corregir una sesión ChatGPT expirada.

## Docker

1. Copia `.env.example` a `.env` y establece un valor privado en `IMAGEGEN_API_TOKEN`.
   Ese mismo valor debe estar configurado como `IMAGEGEN_TOKEN` en el ERP.
2. Ejecuta `docker compose build`.
3. Inicia una sesión ChatGPT propia del servicio con
   `docker compose run --rm imagegen codex login --device-auth` y completa el
   inicio de sesión del proveedor. La sesión queda en el volumen `imagegen-auth`.
4. Ejecuta `docker compose up -d` y comprueba `GET /health` en el puerto 8000.

El puerto se publica en loopback para conectarlo al proxy del VPS. Configura el
proxy con un tiempo de lectura de al menos 500 segundos y conserva la cabecera
Authorization. Si sirve la API bajo `/iadesign-base`, debe eliminar ese prefijo
al reenviar la petición a Uvicorn.

Los volúmenes conservan la sesión OAuth y la configuración al recrear el
contenedor. La sesión debe poder renovarse: no montes `auth.json` como un archivo
de solo lectura. Un token de API de OpenAI no sustituye la sesión ChatGPT que
requiere este CLI. El token privado de la API local y la sesión ChatGPT son dos
credenciales distintas.

## Ejecución sin Docker

Instala `requirements-api.txt` en un virtualenv y arranca Uvicorn desde este
directorio. Configura `IMAGEGEN_API_TOKEN` en el proceso del servidor.

- `IMAGEGEN_CODEX_AUTH_DIR`: directorio opcional con una sesión propia para
  ImageGen (`auth.json` y, si existe, `version.json`). Tiene prioridad.
- `CODEX_HOME`: se respeta cuando no existe la configuración anterior.
- Sin esos ajustes se utiliza el directorio predeterminado `~/.codex`.
- `CHATGPT_IMAGEGEN_BACKEND`: `codex` por defecto en la API; también acepta
  `auto` y `web` cuando ese backend ya esté preparado en la máquina.
- `CHATGPT_IMAGEGEN_MODEL`: modelo Codex disponible para la cuenta ChatGPT del
  servicio. Sin esta variable el CLI usa `gpt-5.5`; si la cuenta lo rechaza,
  prueba `gpt-5.4`. Un HTTP 400 por modelo no compatible no es un timeout.
- `IMAGEGEN_CLI_PATH`: ruta opcional a otra instalación del script Python.
- `XDG_CONFIG_HOME`: directorio escribible para la configuración del CLI cuando
  el directorio predeterminado del usuario no se pueda escribir.

No sobrescribas la sesión que una plataforma proporcione al agente de Codex;
utiliza un directorio propio para el servicio si esa sesión no admite ImageGen.

## Contrato del ERP

`POST /generate` exige `Authorization: Bearer <token privado>` y un formulario
multipart con `prompt`, `size`, `output_format` y hasta cuatro archivos
`references` (PNG, JPEG, WebP o GIF, máximo 10 MB cada uno). Devuelve los bytes
de la imagen con su Content-Type. La generación no aplica estilos personales
guardados en el CLI; las instrucciones y las referencias vienen del ERP.

Los errores usan `{"detail":{"code":"...","message":"..."}}`:

| HTTP | Código | Acción |
| --- | --- | --- |
| 401 | imagegen_unauthorized | Verificar que IMAGEGEN_TOKEN y IMAGEGEN_API_TOKEN coincidan. |
| 503 | imagegen_not_configured | Configurar token, ruta del CLI o backend. |
| 503 | imagegen_auth_required | Renovar la sesión ChatGPT del servicio. |
| 503 | imagegen_model_unsupported | Configurar CHATGPT_IMAGEGEN_MODEL con un modelo disponible para la cuenta. |
| 503 | imagegen_complexity_unsupported | Desplegar el CLI actualizado, que omite el esfuerzo explícito para usar la configuración predeterminada del proveedor. |
| 429 | imagegen_rate_limited | Esperar a que se restablezca la cuota. |
| 504 | imagegen_timeout | Revisar la disponibilidad y los tiempos del proveedor. |
| 504 | imagegen_stalled | Revisar la conexión entre ImageGen y el proveedor. |
| 502 | imagegen_failed / imagegen_invalid_image | Revisar el backend de generación. |

`/health` comprueba el servidor HTTP; una respuesta correcta no acredita una
sesión ChatGPT válida. Verifica también una generación real autenticada. No se
devuelven al cliente los prompts ni la salida completa de error del CLI.

La API ejecuta el CLI sin bloquear el servidor: `/health` sigue respondiendo
durante una generación. Permite 420 segundos de generación y de silencio SSE,
con un límite de 435 segundos para el proceso. El ERP espera hasta 450 segundos;
el proxy necesita admitir al menos 500. El tiempo inicial de conexión del CLI
al proveedor sigue siendo de hasta 30 segundos. Los fallos se registran con
código y segundos transcurridos, sin la salida privada del CLI. Si el proceso
termina con error, el registro también incluye `cli_exit_code`, `reason` y
`provider_status`. `reason` sólo puede ser `provider_http`, `provider_stream`,
`empty_response`, `network`, `invalid_result` o `cli_error`; `provider_status`
sólo contiene un estado HTTP entre 100 y 599 reconocido al inicio de una línea
de error, o `None`. No se registran mensajes del proveedor, prompts ni tokens.

Ante `imagegen_failed`, localiza la referencia mostrada en los logs del ERP y
consulta los logs de ImageGen alrededor de la misma hora para ver esa categoría.
En Dokploy usa los logs de la app ImageGen; si tienes acceso al host Docker:

```bash
docker logs --since 10m <contenedor-imagegen>
```

El rechazo `backend failed mid-generation: Complexity 'low' is not supported
for this image model.` ocurre cuando el proveedor empieza la generación y
rechaza el esfuerzo enviado por el CLI. Cambiarlo por `medium` o `high` puede
seguir siendo rechazado. La corrección omite únicamente `reasoning.effort`, conserva
`reasoning.summary` y deja al proveedor elegir su configuración predeterminada,
tanto para generar desde texto como con referencias.
Hay que desplegar el código actualizado de **ImageGen**; cambiar sólo variables
del ERP no modifica ese payload. Este rechazo no requiere renovar la sesión ni
cambiar el modelo de la cuenta.

Tras desplegar ImageGen, comprueba una generación real con el comando siguiente
y desde IA Design, incluido el guardado del resultado en el historial. Las
pruebas locales con proveedor simulado no acreditan que el despliegue ya se haya
actualizado ni que la generación de producción haya terminado correctamente.

Ejecuta la prueba en el contenedor de ImageGen con su configuración actual; al
omitir `--model` se conserva `CHATGPT_IMAGEGEN_MODEL` o el valor predeterminado
del CLI:

```bash
python /app/chatgpt-imagegen "Un círculo azul sobre fondo blanco" --backend codex --size 1024x1024 --format png --timeout 420 --stall-timeout 420 --quiet --no-progress --no-style -o /tmp/ia-prueba.png
```

La versión original devuelve stderr dentro de `detail`. Su línea normal de
progreso contiene `timeout=420s`; no debe interpretarse como un timeout. La API
actual distingue los fallos reconocidos y devuelve códigos estables.

## Pruebas

Instala `requirements-test.txt` y ejecuta
`python -m unittest test_chatgpt_imagegen test_api -v`. Las pruebas de la API
usan un CLI de prueba en un proceso real; no consumen generación de ChatGPT.
