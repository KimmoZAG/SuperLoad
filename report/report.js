const profileSwitch = document.querySelector("[data-switch]");
const profiles = [...document.querySelectorAll("[data-profile]")];

function selectProfile(profile) {
  for (const panel of profiles) {
    panel.hidden = panel.dataset.profile !== profile;
  }
  for (const button of profileSwitch.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.target === profile));
  }
}

profileSwitch.hidden = false;
profileSwitch.addEventListener("click", (event) => {
  const button = event.target.closest("[data-target]");
  if (button) selectProfile(button.dataset.target);
});
selectProfile("interactive-prefix");

for (const chart of document.querySelectorAll(".chart")) {
  const metric = chart.querySelector(".metric");
  const title = chart.querySelector("[data-metric-title]");
  const rows = [...chart.querySelectorAll(".bar-row")];
  metric.hidden = false;
  metric.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const key = button.dataset.metric;
    const high = Math.max(...rows.map((row) => Number(row.dataset[key])));
    const gigabytes = key === "gib";
    title.textContent = gigabytes ? "逻辑吞吐 (GiB/s) · 越高越好" : "P95 就绪延迟 (ms) · 越低越好";
    for (const row of rows) {
      const value = Number(row.dataset[key]);
      row.querySelector(".bar-fill").style.setProperty("--size", (value / high * 100) + "%");
      row.querySelector(".value").textContent = value.toFixed(2);
    }
    for (const option of metric.querySelectorAll("button")) {
      option.setAttribute("aria-pressed", String(option === button));
    }
  });
}
