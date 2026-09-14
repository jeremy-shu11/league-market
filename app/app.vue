<script setup lang="ts">
import legacyDocument from "../frontend/index.html?raw";
import "../frontend/styles.css";

declare global {
  interface Window {
    __leagueMarketRuntimeLoaded?: boolean;
    __leagueMarketRuntimeLoading?: Promise<void>;
    __leagueMarketToast?: (message: string, tone?: string) => boolean;
  }
}

const legacyShell = extractLegacyShell(legacyDocument);
const toast = useToast();

useHead({
  title: "The League Market",
  meta: [
    { name: "viewport", content: "width=device-width, initial-scale=1, viewport-fit=cover" },
    { name: "league-market-asset-version", content: "__ASSET_VERSION__" }
  ],
  script: [
    {
      key: "league-market-theme",
      innerHTML: `try{document.documentElement.dataset.theme=localStorage.getItem("leagueMarketTheme")||"dark"}catch(_){document.documentElement.dataset.theme="dark"}`
    }
  ]
});

function extractLegacyShell(documentHtml: string) {
  const body = documentHtml.match(/<body[^>]*>([\s\S]*)<\/body>/i)?.[1] || "";
  return body.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").trim();
}

function assetQuery() {
  const version = document.querySelector<HTMLMetaElement>("meta[name='league-market-asset-version']")?.content;
  if (!version || version === "__ASSET_VERSION__") return "";
  return `?v=${encodeURIComponent(version)}`;
}

function loadScript(src: string, id: string) {
  const existing = document.getElementById(id) as HTMLScriptElement | null;
  if (existing) return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.id = id;
    script.src = src;
    script.async = false;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Unable to load ${src}`));
    document.body.appendChild(script);
  });
}

async function loadMarketRuntime() {
  if (window.__leagueMarketRuntimeLoaded) return;
  if (window.__leagueMarketRuntimeLoading) return window.__leagueMarketRuntimeLoading;
  const query = assetQuery();
  window.__leagueMarketRuntimeLoading = (async () => {
    await loadScript(`/static/vendor/chart.umd.js${query}`, "league-market-chart");
    await loadScript(`/static/vendor/lucide.min.js${query}`, "league-market-icons");
    await loadScript(`/static/app.js${query}`, "league-market-runtime");
    window.__leagueMarketRuntimeLoaded = true;
  })();
  return window.__leagueMarketRuntimeLoading;
}

onMounted(() => {
  document.documentElement.dataset.theme = localStorage.getItem("leagueMarketTheme") || "dark";
  window.__leagueMarketToast = (message, tone = "info") => {
    const color = tone === "success" ? "success" : tone === "warn" ? "warning" : tone === "error" ? "error" : "info";
    const icon = tone === "success" ? "i-lucide-circle-check" : tone === "warn" ? "i-lucide-triangle-alert" : tone === "error" ? "i-lucide-circle-alert" : "i-lucide-info";
    toast.add({ title: message, color, icon, duration: 5200 });
    return true;
  };
  loadMarketRuntime().catch((error) => {
    toast.add({
      title: "Market runtime did not load",
      description: error instanceof Error ? error.message : "Refresh the page and try again.",
      color: "error",
      icon: "i-lucide-circle-alert",
      duration: 0
    });
  });
});
</script>

<template>
  <UApp :toaster="{ position: 'bottom-right', duration: 5200 }" :tooltip="{ delayDuration: 350 }">
    <div class="league-market-nuxt-bridge" v-html="legacyShell" />
  </UApp>
</template>
