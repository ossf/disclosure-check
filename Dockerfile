FROM python:3.11-bullseye@sha256:2931a51a6a3add7ea59534fa8bd039c1b86b8b6fbfbacbd04647ee45e37c486a

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
