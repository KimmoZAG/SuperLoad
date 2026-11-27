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

function showArchivedSection() {
  const archive = document.querySelector("[data-archive]");
  const target = document.getElementById(window.location.hash.slice(1));
  if (target && archive.contains(target)) {
    archive.open = true;
    target.scrollIntoView();
  }
}

showArchivedSection();
window.addEventListener("hashchange", showArchivedSection);
