// The visual config editor, moved out of vimar-intercom-card.js as is.

const DEFAULTS = {
  name: "Citofono",
  camera: "camera.vimar_intercom_intercom",
  status: "sensor.vimar_intercom_intercom_stato",
  lock: "lock.vimar_intercom_serratura",
  last_ring: "sensor.vimar_intercom_intercom_ultimo_squillo",
  anchor: "citofono",
  history: 8,
  layout: "overlay",  // o "sotto" o "popup"
  compact_style: "pillola",  // card compatta del layout popup: o "tile"
  idle_picture: "last_ring",  // o "standby": da fermo niente foto dell'ultimo squillo
  confirm_open: true,
  listen_on_ring: false,
};

// Editor visuale: un ha-form di HA con cinque campi. Le altre chiavi (status, lock, last_ring,
// anchor) restano in YAML e passano intatte. Un campo svuotato o lasciato al default non finisce in YAML.
const EDITOR_TAG = "vimar-intercom-card-editor";
const SCHEMA = [
  { name: "camera", selector: { entity: { domain: "camera" } } },
  { name: "name", selector: { text: {} } },
  { name: "layout", selector: { select: { mode: "dropdown", options: [
    { value: "overlay", label: "Video a tutta card" },
    { value: "sotto", label: "Tasti sotto il video" },
    { value: "popup", label: "Popup al tocco" },
  ] } } },
  { name: "compact_style", selector: { select: { mode: "dropdown", options: [
    { value: "pillola", label: "Pillola" },
    { value: "tile", label: "Tile" },
  ] } } },
  { name: "idle_picture", selector: { select: { mode: "dropdown", options: [
    { value: "last_ring", label: "Foto dell'ultimo squillo" },
    { value: "standby", label: "Icona del citofono" },
  ] } } },
  { name: "shortcuts", selector: { entity: { multiple: true, domain: ["lock", "button"] } } },
  { name: "history", selector: { number: { min: 0, max: 50, mode: "box" } } },
  { name: "confirm_open", selector: { boolean: {} } },
  { name: "listen_on_ring", selector: { boolean: {} } },
];
const FIELD = { camera: "Telecamera", name: "Nome", layout: "In diretta", compact_style: "Card compatta (layout popup)", idle_picture: "Da fermo",
  shortcuts: "Scorciatoie Apri sulla card compatta (vuoto = serratura)", history: "Squilli in cronologia (0 = niente)",
  confirm_open: "Apri con doppio tocco", listen_on_ring: "Ascolta il visitatore durante lo squillo" };

class VimarIntercomCardEditor extends HTMLElement {
  setConfig(config) {
    this._cfg = config;
    this._sync();
  }

  set hass(hass) {
    this._hass = hass;
    this._sync();
  }

  _sync() {
    if (!this._hass || !this._cfg) return;  // il selettore entità vuole hass
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = SCHEMA;
      this._form.computeLabel = (s) => FIELD[s.name] || s.name;
      this._form.addEventListener("value-changed", (e) => {
        e.stopPropagation();
        const config = { ...this._cfg };
        for (const { name } of SCHEMA) {
          const v = e.detail.value[name];
          if (v === undefined || v === "" || v === DEFAULTS[name] || v?.length === 0) delete config[name]; else config[name] = v;
        }
        this._cfg = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.append(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = { ...DEFAULTS, ...this._cfg };
  }
}

export { DEFAULTS, EDITOR_TAG, VimarIntercomCardEditor };
