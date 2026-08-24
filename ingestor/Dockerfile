FROM python:3.12-slim

WORKDIR /app

# h5py and pyproj ship manylinux wheels, so no build toolchain is needed.
COPY ingestor/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ingestor/ ./

ENV FRAME_DIR=/data
CMD ["python", "-u", "-m", "ingestor.main"]
