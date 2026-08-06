FROM python:3.12-alpine

RUN apk add --no-cache docker-cli docker-cli-compose

ENV PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "docker_manage_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
