// Card for SMN's cameras. Animated cameras (radar, satellite) play the
// looping MP4 their entity_picture points to (smoother than their GIF camera
// image); the alerts map shows its camera image. Configurable from the visual
// editor.
//
//   type: custom:smn-ar-video-card
//   entity: camera.cordoba_satelite_provincia_infrarrojo
//   name: Satélite               # optional, defaults to the entity's name
//   name_position: below         # below | overlay | hidden
//   aspect_ratio: auto           # auto | 16:9 | 4:3 | 1:1 | 3:4
//   fit: cover                   # cover | contain (with a fixed aspect_ratio)
//   tap_action: play_pause       # play_pause | more_info | fullscreen | none
//   autoplay: true
//   playback_rate: 1             # 0.5 | 0.75 | 1 | 1.5 | 2
//   show_controls: false         # the browser's own video controls
//   show_updated: true           # when the animation was last rebuilt

const VIDEO_PATH = "/api/smn_ar/video/";

const DEFAULTS = {
  name_position: "below",
  aspect_ratio: "auto",
  fit: "cover",
  tap_action: "play_pause",
  autoplay: true,
  playback_rate: 1,
  show_controls: false,
  show_updated: true,
};

const option = (value, label) => ({ value, label });

const SCHEMA = [
  {
    name: "entity",
    required: true,
    selector: { entity: { filter: { domain: "camera", integration: "smn_ar" } } },
  },
  { name: "name", selector: { text: {} } },
  {
    name: "",
    type: "grid",
    schema: [
      {
        name: "name_position",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              option("below", "Debajo"),
              option("overlay", "Sobre la imagen"),
              option("hidden", "Oculto"),
            ],
          },
        },
      },
      {
        name: "tap_action",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              option("play_pause", "Pausar / reanudar"),
              option("more_info", "Abrir detalle"),
              option("fullscreen", "Pantalla completa"),
              option("none", "Nada"),
            ],
          },
        },
      },
      {
        name: "aspect_ratio",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              option("auto", "Original"),
              option("16:9", "16:9 (apaisado)"),
              option("4:3", "4:3"),
              option("1:1", "1:1 (cuadrado)"),
              option("3:4", "3:4 (vertical)"),
            ],
          },
        },
      },
      {
        name: "fit",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              option("cover", "Llenar (recorta los bordes)"),
              option("contain", "Ajustar (sin recortar)"),
            ],
          },
        },
      },
      {
        name: "playback_rate",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              option("0.5", "0.5× (lenta)"),
              option("0.75", "0.75×"),
              option("1", "1× (normal)"),
              option("1.5", "1.5×"),
              option("2", "2× (rápida)"),
            ],
          },
        },
      },
    ],
  },
  {
    name: "",
    type: "grid",
    schema: [
      { name: "autoplay", selector: { boolean: {} } },
      { name: "show_updated", selector: { boolean: {} } },
      { name: "show_controls", selector: { boolean: {} } },
    ],
  },
];

const LABELS = {
  entity: "Cámara de SMN",
  name: "Nombre (opcional)",
  name_position: "Nombre",
  tap_action: "Al tocar",
  aspect_ratio: "Proporción",
  fit: "Encuadre",
  playback_rate: "Velocidad",
  autoplay: "Reproducir solo",
  show_updated: "Mostrar hora de actualización",
  show_controls: "Controles de video",
};

const HELPERS = {
  fit: "Solo aplica con una proporción fija.",
  autoplay: "Si está apagado, muestra la imagen fija hasta que la toques.",
};

const formatTime = (iso) => {
  const date = new Date(iso);
  return isNaN(date)
    ? ""
    : date.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
};

class SmnArVideoCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("smn-ar-video-card-editor");
  }

  static getStubConfig(hass) {
    const cameras = Object.keys(hass.states).filter(
      (id) =>
        id.startsWith("camera.") &&
        (hass.entities?.[id]?.platform === "smn_ar" ||
          (hass.states[id].attributes.entity_picture || "").startsWith(VIDEO_PATH)),
    );
    const satellite = cameras.find((id) =>
      (hass.states[id].attributes.entity_picture || "").startsWith(VIDEO_PATH),
    );
    return { entity: satellite || cameras[0] || "" };
  }

  setConfig(config) {
    if (!config || !config.entity || !config.entity.startsWith("camera.")) {
      throw new Error("Elegí una cámara de SMN");
    }
    this._config = { ...DEFAULTS, ...config };
    this._playing = this._config.autoplay !== false;
    this._build();
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 5;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 4 };
  }

  _build() {
    const config = this._config;
    const [w, h] = (config.aspect_ratio || "auto").split(":").map(Number);
    const fixedRatio = w > 0 && h > 0;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; height: 100%; }
        .media { position: relative; line-height: 0; background: #000; }
        .media.clickable { cursor: pointer; }
        .media.fixed { aspect-ratio: ${fixedRatio ? `${w} / ${h}` : "auto"}; }
        img, video { width: 100%; display: block; }
        .fixed img, .fixed video { height: 100%; object-fit: ${config.fit === "contain" ? "contain" : "cover"}; }
        video { display: none; }
        .has-video img { display: none; }
        .has-video video { display: block; }
        .play {
          position: absolute; inset: 0; margin: auto;
          width: 64px; height: 64px; border-radius: 50%;
          background: rgba(0, 0, 0, 0.55); color: #fff;
          display: none; align-items: center; justify-content: center;
          --mdc-icon-size: 40px; pointer-events: none;
        }
        .can-play.paused .play { display: flex; }
        .footer {
          display: flex; align-items: baseline; justify-content: space-between;
          gap: 8px; padding: 8px 16px; line-height: normal;
        }
        .footer.overlay {
          position: absolute; left: 0; right: 0; bottom: 0;
          color: #fff; background: linear-gradient(transparent, rgba(0, 0, 0, 0.7));
          padding-top: 24px; pointer-events: none;
        }
        .footer.hidden-name .name { display: none; }
        .footer.empty { display: none; }
        .name { font-size: 1.1em; }
        .updated { font-size: 0.85em; opacity: 0.75; white-space: nowrap; }
        .updated:empty { display: none; }
      </style>
      <ha-card>
        <div class="media ${fixedRatio ? "fixed" : ""}">
          <img alt="" />
          <video muted loop playsinline></video>
          <div class="play"><ha-icon icon="mdi:play"></ha-icon></div>
          ${config.name_position === "overlay" ? this._footerHtml("overlay") : ""}
        </div>
        ${config.name_position !== "overlay" ? this._footerHtml(config.name_position === "hidden" ? "hidden-name" : "") : ""}
      </ha-card>`;
    const root = this.shadowRoot;
    this._media = root.querySelector(".media");
    this._img = root.querySelector("img");
    this._video = root.querySelector("video");
    this._footer = root.querySelector(".footer");
    this._name = root.querySelector(".name");
    this._updated = root.querySelector(".updated");
    this._video.controls = Boolean(config.show_controls);
    this._video.defaultPlaybackRate = Number(config.playback_rate) || 1;
    this._video.addEventListener("play", () => this._setPlaying(true));
    this._video.addEventListener("pause", () => this._setPlaying(false));
    this._videoSrc = null;
    this._stillSrc = null;
    this._version = undefined;
    this._media.addEventListener("click", (ev) => this._onTap(ev));
  }

  _footerHtml(extraClass) {
    return `<div class="footer ${extraClass}"><span class="name"></span><span class="updated"></span></div>`;
  }

  _update() {
    if (!this._config || !this._media) return;
    const stateObj = this._hass && this._hass.states[this._config.entity];
    if (!stateObj) {
      this._name.textContent = `Entidad no encontrada: ${this._config.entity}`;
      return;
    }
    const picture = stateObj.attributes.entity_picture || "";
    const hasVideo = picture.startsWith(VIDEO_PATH);
    const version = stateObj.attributes.animation_updated;

    this._name.textContent = this._config.name || stateObj.attributes.friendly_name || "";
    this._updated.textContent =
      this._config.show_updated && version ? `Actualizado ${formatTime(version)}` : "";
    this._footer.classList.toggle(
      "empty",
      this._config.name_position === "hidden" && !this._updated.textContent,
    );

    // The camera image: the animated camera's GIF until the video loads, or the
    // whole image for the other cameras. Its URL changes with each state
    // write (new image, or the access token rotating), so it stays fresh.
    const token = new URL(picture, location.origin).searchParams.get("token");
    const still = `/api/camera_proxy/${this._config.entity}?token=${token}&t=${encodeURIComponent(stateObj.last_updated)}`;
    if (this._stillSrc !== still) {
      this._stillSrc = still;
      this._img.src = still;
    }

    this._media.classList.toggle("can-play", hasVideo);
    this._media.classList.toggle("clickable", this._tapAction(hasVideo) !== "none");
    if (!hasVideo) {
      this._videoSrc = null;
      this._media.classList.remove("has-video");
      return;
    }
    // The token in the URL rotates every few minutes, so a new video is
    // only fetched when the animation itself was rebuilt.
    this._videoSrc = picture;
    if (this._playing && (!this._video.getAttribute("src") || version !== this._version)) {
      this._start();
    } else if (!this._playing && !this._video.getAttribute("src")) {
      // Paused from the start (autoplay off): show the video's first frame,
      // not the GIF, which would animate anyway.
      this._video.preload = "auto";
      this._video.src = this._videoSrc;
      this._media.classList.add("has-video");
    }
    this._version = version;
    this._media.classList.toggle("paused", !this._playing);
  }

  _tapAction(hasVideo) {
    const action = this._config.tap_action;
    if (action === "play_pause" && (!hasVideo || this._config.show_controls)) {
      // Nothing to pause on a still camera, and the controls already do it.
      return this._config.show_controls && hasVideo ? "none" : "more_info";
    }
    return action;
  }

  _onTap(ev) {
    const action = this._tapAction(Boolean(this._videoSrc));
    if (action === "play_pause") {
      if (this._playing) {
        this._video.pause();
      } else {
        this._start();
      }
    } else if (action === "more_info") {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          detail: { entityId: this._config.entity },
          bubbles: true,
          composed: true,
        }),
      );
    } else if (action === "fullscreen") {
      if (ev.target === this._video && this._config.show_controls) return;
      const target = this._videoSrc && this._playing ? this._video : this._media;
      if (target.requestFullscreen) {
        target.requestFullscreen().catch(() => {});
      } else if (target.webkitEnterFullscreen) {
        target.webkitEnterFullscreen(); // iOS: only <video> can go fullscreen
      }
    }
  }

  _setPlaying(playing) {
    this._playing = playing;
    this._media.classList.toggle("paused", !playing);
  }

  _start() {
    // Restarting after a pause reloads the URL, picking up the newest video.
    this._video.src = this._videoSrc;
    this._video.playbackRate = Number(this._config.playback_rate) || 1;
    this._media.classList.add("has-video");
    this._setPlaying(true);
    this._video.play().catch(() => this._setPlaying(false));
  }
}

// ha-form (and the entity picker it uses) are loaded lazily by the frontend;
// opening a built-in card's editor forces them in.
const loadHaForm = async () => {
  if (customElements.get("ha-form") && customElements.get("ha-selector")) return;
  const helpers = await window.loadCardHelpers?.();
  if (!helpers) return;
  const card = await helpers.createCardElement({ type: "entities", entities: [] });
  await card.constructor.getConfigElement();
};

class SmnArVideoCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) {
      this._form.hass = hass;
    } else {
      this._render();
    }
  }

  async _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      if (this._loading) return;
      this._loading = true;
      await loadHaForm();
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => LABELS[schema.name] ?? schema.name;
      this._form.computeHelper = (schema) => HELPERS[schema.name];
      this._form.addEventListener("value-changed", (ev) => this._changed(ev.detail.value));
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = SCHEMA;
    this._form.data = {
      ...DEFAULTS,
      ...this._config,
      playback_rate: String(this._config.playback_rate ?? DEFAULTS.playback_rate),
    };
  }

  _changed(value) {
    // Keep the YAML short: only what differs from the defaults.
    const config = { type: this._config.type || "custom:smn-ar-video-card" };
    for (const [key, raw] of Object.entries(value)) {
      if (key === "type") continue;
      const val = key === "playback_rate" ? Number(raw) : raw;
      if (val === "" || val === undefined || val === null || DEFAULTS[key] === val) continue;
      config[key] = val;
    }
    this._config = config;
    this.dispatchEvent(
      new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }),
    );
  }
}

customElements.define("smn-ar-video-card", SmnArVideoCard);
customElements.define("smn-ar-video-card-editor", SmnArVideoCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "smn-ar-video-card",
  name: "SMN Mapa / Satélite",
  description: "Cámaras de SMN: animación satelital en loop, radar o mapa de avisos.",
  preview: true,
  documentationURL: "https://github.com/pabloantonelli/smn-ar-ha",
});
