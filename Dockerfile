FROM python:3.12-bullseye@sha256:6e87056b8515716219d318ac8c37d07c57b94a931306c19e90cdf0d529ec47dd

ARG OSSGADGET_VERSION="0.1.406"

WORKDIR /app

COPY dist/disclosurecheck-*.tar.gz /app/

RUN cd /app && \
    pip install disclosurecheck-*.tar.gz

# Install OSS Gadget
# License: MIT
RUN cd /opt && \
    wget -q https://github.com/microsoft/OSSGadget/releases/download/v${OSSGADGET_VERSION}/OSSGadget_linux_${OSSGADGET_VERSION}.tar.gz -O OSSGadget.tar.gz && \
    tar zxvf OSSGadget.tar.gz && \
    rm OSSGadget.tar.gz && \
    mv OSSGadget_linux_${OSSGADGET_VERSION} OSSGadget && \
    sed -i 's@${currentdir}@/tmp@' OSSGadget/nlog.config

ENV PATH="$PATH:/opt/OSSGadget"

ENTRYPOINT ["disclosurecheck"]
