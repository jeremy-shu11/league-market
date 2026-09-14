export default defineNuxtConfig({
  modules: ["@nuxt/ui"],
  srcDir: "app/",
  ui: {
    fonts: false
  },
  css: ["~/assets/css/nuxt-ui.css"],
  compatibilityDate: "2026-09-08",
  devtools: { enabled: true },
  nitro: {
    preset: "static"
  },
  app: {
    head: {
      charset: "utf-8",
      viewport: "width=device-width, initial-scale=1",
      htmlAttrs: {
        lang: "en"
      }
    }
  },
  vite: {
    server: {
      proxy: {
        "/api": {
          target: "http://127.0.0.1:5065",
          changeOrigin: true
        }
      }
    }
  }
});
