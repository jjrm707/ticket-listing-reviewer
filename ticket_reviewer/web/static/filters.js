(() => {
  const form = document.querySelector("form.filters");
  if (!form) return;
  form.addEventListener("formdata", (event) => {
    for (const [name, value] of event.formData.entries()) {
      if (value === "") event.formData.delete(name);
    }
  });
})();
