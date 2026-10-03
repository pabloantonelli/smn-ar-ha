// Card for SMN's satellite cameras: plays the looping MP4 the camera's
// entity_picture points to (a camera image is rendered in an <img>, which
// only Safari can play an MP4 in), with the camera's still image until the
// video is available. Tapping it pauses/resumes.
//
//   type: custom:smn-ar-video-card
//   entity: camera.smn_satelite_argentina
//   name: Satélite   # optional
//   autoplay: false  # optional: show the still and play on tap

const VIDEO_PATH = "/api/smn_ar/video/";

class SmnArVideoCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity || !config.entity.startsWith("camera.")) {
      throw new Error("Configurá `entity` con una cámara de SMN");
    }
    this._config = config;
    this._playing = config.autoplay !== false;
    this._build();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig(hass) {
    const camera = Object.keys(hass.states).find(
      (id) =>
        id.startsWith("camera.") &&
        (hass.states[id].attributes.entity_picture || "").startsWith(VIDEO_PATH),
    );
    return { entity: camera || "" };
  }

  _build() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .media { position: relative; cursor: pointer; line-height: 0; }
        img, video { width: 100%; display: block; }
        video { display: none; }
        .has-video img { display: none; }
        .has-video video { display: block; }
        .play {
          position: absolute; inset: 0; margin: auto;
          width: 64px; height: 64px; border-radius: 50%;
          background: rgba(0, 0, 0, 0.55); color: #fff;
          display: none; align-items: center; justify-content: center;
          --mdc-icon-size: 40px;
        }
        .paused .play { display: flex; }
        .name { padding: 8px 16px; font-size: 1.1em; line-height: normal; }
        .name:empty { display: none; }
      </style>
      <ha-card>
        <div class="media">
          <img alt="" />
          <video muted loop playsinline></video>
          <div class="play"><ha-icon icon="mdi:play"></ha-icon></div>
        </div>
        <div class="name"></div>
      </ha-card>`;
    this._media = this.shadowRoot.querySelector(".media");
    this._img = this.shadowRoot.querySelector("img");
    this._video = this.shadowRoot.querySelector("video");
    this._name = this.shadowRoot.querySelector(".name");
    this._videoSrc = null;
    this._media.addEventListener("click", () => this._toggle());
  }

  _update() {
    const stateObj = this._hass && this._hass.states[this._config.entity];
    if (!stateObj) {
      this._name.textContent = `Entidad no encontrada: ${this._config.entity}`;
      return;
    }
    const picture = stateObj.attributes.entity_picture || "";
    const hasVideo = picture.startsWith(VIDEO_PATH);
    this._name.textContent =
      this._config.name ?? stateObj.attributes.friendly_name ?? "";

    // The still, shown until the video exists (or while paused before the
    // first play). Its URL changes with each state write, so it follows
    // new animations.
    const token = new URL(picture, location.origin).searchParams.get("token");
    const still = `/api/camera_proxy/${this._config.entity}?token=${token}&t=${encodeURIComponent(stateObj.last_updated)}`;
    if (this._stillSrc !== still) {
      this._stillSrc = still;
      this._img.src = still;
    }

    if (!hasVideo) {
      this._videoSrc = null;
      this._media.classList.remove("has-video");
      this._media.classList.toggle("paused", !this._playing);
      return;
    }
    // The token in the URL rotates every few minutes, so a new video is
    // only fetched when the animation itself was rebuilt.
    this._videoSrc = picture;
    const version = stateObj.attributes.animation_updated;
    if (this._playing && (!this._video.getAttribute("src") || version !== this._version)) {
      this._start();
    }
    this._version = version;
    this._media.classList.toggle("paused", !this._playing);
  }

  _toggle() {
    this._playing = !this._playing;
    if (!this._playing) {
      this._video.pause();
    } else if (this._videoSrc) {
      this._start();
    }
    this._media.classList.toggle("paused", !this._playing);
  }

  _start() {
    // Restarting after a pause reloads the URL, picking up the newest video.
    this._video.src = this._videoSrc;
    this._media.classList.add("has-video");
    this._video.play().catch(() => {});
  }
}

customElements.define("smn-ar-video-card", SmnArVideoCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "smn-ar-video-card",
  name: "SMN Satélite (video)",
  description: "Animación satelital en loop, que se pausa al tocarla.",
});
