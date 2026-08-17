FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir -e .

COPY config.example.yaml seeds.example.txt ./

ENTRYPOINT ["asmrlib-archiver"]
CMD ["--help"]

