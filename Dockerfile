FROM python:3.11-slim

WORKDIR /app
COPY relay_server.py .

# PORT is injected by Railway / Render / Fly.io at runtime
ENV PORT=7777
EXPOSE 7777

CMD ["python", "relay_server.py"]
