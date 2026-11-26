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
