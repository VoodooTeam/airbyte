FROM python:3.10-slim

# Bash is installed for more convenient debugging.
# Include socat binary in the connector image
RUN apt-get update && apt-get install -y bash && apt-get install -y socat && rm -rf /var/lib/apt/lists/*

WORKDIR /airbyte/integration_code
COPY source_facebook_marketing ./source_facebook_marketing
COPY main.py ./main.py
COPY poetry.lock ./poetry.lock
COPY pyproject.toml ./pyproject.toml
COPY README.md ./README.md

ENV POETRY_VIRTUALENVS_CREATE=false

RUN pip install poetry
RUN poetry install

ENV AIRBYTE_ENTRYPOINT "python /airbyte/integration_code/main.py"
ENTRYPOINT ["python", "/airbyte/integration_code/main.py"]

LABEL io.airbyte.version=1.1.16
LABEL io.airbyte.name=airbyte/source-facebook-marketing
