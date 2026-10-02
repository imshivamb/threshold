# ring-sandbox emulator: a local Ring Partner API with signed webhooks and scenarios,
# plus ringsynth for writing weeks of persona history into it.
# Build context: the repo root (see docker-compose.yml).
FROM python:3.12-slim
RUN pip install --no-cache-dir "ring-sandbox[server]==0.5.0"
COPY tools/synth /opt/synth
RUN pip install --no-cache-dir /opt/synth
ENV PERSONAS=/opt/synth/personas
EXPOSE 8787
CMD ["ring-sandbox", "serve", "--host", "0.0.0.0", "--port", "8787"]
