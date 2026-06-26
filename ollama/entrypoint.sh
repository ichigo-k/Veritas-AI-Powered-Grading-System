#!/usr/bin/env bash
# Start the Ollama server, wait for it, pull configured models, then stay up.
set -e

OLLAMA_BIN=/bin/ollama

# 1. Start the server in the background.
$OLLAMA_BIN serve &
server_pid=$!

# 2. Wait until the API is responding before pulling.
echo "Waiting for Ollama server to become ready..."
until $OLLAMA_BIN list >/dev/null 2>&1; do
  sleep 1
done
echo "Ollama server is ready."

# 3. Pull each configured model (space-separated). Already-present models
#    are a fast no-op, so this is safe on every boot.
for model in ${OLLAMA_PULL_MODELS:-llama3.2:3b}; do
  echo "Pulling model: $model"
  $OLLAMA_BIN pull "$model"
done
echo "All models ready."

# 4. Hand control back to the server process (keeps the container alive and
#    forwards signals for clean shutdown).
wait "$server_pid"
