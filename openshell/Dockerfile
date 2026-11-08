FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd sandbox && useradd -g sandbox -m sandbox && install -d -o sandbox -g sandbox /sandbox
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /sandbox
USER sandbox
CMD ["sleep", "infinity"]
