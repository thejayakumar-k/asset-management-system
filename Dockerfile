FROM odoo:19.0

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache

RUN apt-get update --fix-missing -y && \
    apt-get install -y --fix-missing snmp snmp-mibs-downloader iputils-ping && \
    download-mibs && \
    pip3 install --break-system-packages \
    icmplib \
    qrcode \
    xlsxwriter \
    requests \
    pillow

USER odoo