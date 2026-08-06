module.exports = {
  content: ["./templates/**/*.html", "./static/**/*.js"],
  theme: {
    extend: {
      colors: { brandt: { DEFAULT: "#008542", light: "#16b364", dark: "#005f31" } },
      fontFamily: { sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"] }
    }
  }
};
