FROM gradle:8.10-jdk17 AS plugin-build
# youtube-source PR #229 (feat/sabr-support): remote poToken + SABR fallback,
# needed because 1.18.2 (latest release) is fully walled by YouTube as of 2026-08-18
ARG YTS_COMMIT=99ca38e58f95bdda822057b7d5cf6d35997a3867
RUN git clone https://github.com/ashton045/youtube-source.git /src \
 && git -C /src checkout ${YTS_COMMIT}
WORKDIR /src
RUN gradle :plugin:build -x test --no-daemon

FROM ghcr.io/lavalink-devs/lavalink:4.2.2-alpine@sha256:96be2be7ee50d35a9bd42c8c7b99e2a4b741f09123066c1ebb9e014dd7db204d

COPY --chown=322:322 application.yml /opt/Lavalink/application.yml
COPY --from=plugin-build --chown=322:322 /src/plugin/build/libs/youtube-plugin-*.jar /opt/Lavalink/plugins/
