ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python 3 and system dependencies for Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

# Install Chromium and its system deps via playwright
RUN playwright install chromium --with-deps

COPY armadillo.py webui.py run.sh ./
RUN chmod +x run.sh

CMD ["/app/run.sh"]
