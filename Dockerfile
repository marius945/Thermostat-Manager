ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and dependencies
RUN apk add --no-cache python3 py3-pip

# Create app directory
WORKDIR /app

# Copy application files
COPY rootfs/app /app

# Install Python packages
RUN pip3 install --no-cache-dir --break-system-packages \
    flask==3.0.0 \
    requests==2.31.0 \
    werkzeug==3.0.1

# Expose port
EXPOSE 5000

# Run the application
CMD ["python3", "/app/main.py"]
