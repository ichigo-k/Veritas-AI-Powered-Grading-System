# Ollama service for Railway.
#
# Wraps the official ollama/ollama image with an entrypoint that pulls the
# configured model(s) on startup, then keeps the server running. Models are
# cached in /root/.ollama — mount a Railway volume there so they survive
# redeploys (otherwise the model re-downloads on every boot).

FROM ollama/ollama:latest

# Bind to all interfaces so sibling Railway services can reach it over the
# private network (Railway's internal network is IPv6; "::" covers both).
ENV OLLAMA_HOST=::

# Space-separated list of models to pull on startup. Override in Railway.
ENV OLLAMA_PULL_MODELS="llama3.2:3b"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 11434

# Override the base image's entrypoint (which is just `ollama`).
ENTRYPOINT ["/entrypoint.sh"]
