FROM gradle:8.10-jdk17 AS plugin-build
# youtube-source PR #229 (remote poToken + SABR fallback), plus our own commit
# adding an innertube relay: YouTube answers LOGIN_REQUIRED to /player from
# Railway's address range whatever credentials we send, so the API calls have to
# leave from elsewhere while the audio keeps streaming from here. See relay/.
ARG YTS_COMMIT=79a006058328947a6ed420fa53fa86c6a07f53cf
RUN git clone https://github.com/Aegonex/youtube-source.git /src \
 && git -C /src checkout ${YTS_COMMIT}
WORKDIR /src
RUN gradle :plugin:build -x test --no-daemon

FROM ghcr.io/lavalink-devs/lavalink:4.2.2-alpine@sha256:96be2be7ee50d35a9bd42c8c7b99e2a4b741f09123066c1ebb9e014dd7db204d

COPY --chown=322:322 application.yml /opt/Lavalink/application.yml
COPY --from=plugin-build --chown=322:322 /src/plugin/build/libs/youtube-plugin-*.jar /opt/Lavalink/plugins/
