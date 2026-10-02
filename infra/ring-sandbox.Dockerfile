# ring-sandbox emulator: a local Ring Partner API with signed webhooks and scenarios.
FROM python:3.12-slim
RUN pip install --no-cache-dir "ring-sandbox[server]==0.5.0"
EXPOSE 8787
CMD ["ring-sandbox", "serve", "--host", "0.0.0.0", "--port", "8787"]
