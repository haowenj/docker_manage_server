FROM docker:27-cli

RUN apk add --no-cache docker-cli-compose python3 py3-pip \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "docker_manage_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
