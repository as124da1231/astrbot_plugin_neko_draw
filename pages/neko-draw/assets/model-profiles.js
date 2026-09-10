export function inferredProtocol(url = "") {
  const value = String(url).toLowerCase();
  if (value.includes("wavespeed")) return "WaveSpeed";
  if (value.includes("runninghub")) return "RunningHub";
  return "OpenAI";
}

export function recommendedModelConfig(providerUrl, modelId, mode = "text") {
  const id = String(modelId || "").toLowerCase();
  const url = String(providerUrl || "").toLowerCase();
  const protocol = inferredProtocol(providerUrl);
  const editing = mode === "edit";
  const result = { label: "通用 OpenAI 图像", params: { _endpoint: editing ? "images/edits" : "images/generations", size: "1024x1024", n: 1 }, refer_field: editing ? "images" : "", max_refer_images: editing ? 10 : 0 };
  if (url.includes("siliconflow")) {
    result.label = "SiliconFlow 图像";
    result.params = { _endpoint: "images/generations", image_size: "1328x1328", batch_size: 1, num_inference_steps: 20, guidance_scale: 7.5 };
    result.refer_field = editing ? "image" : "";
    result.max_refer_images = editing ? 1 : 0;
  } else if (id.includes("gpt-image") || id.includes("dall-e")) {
    result.label = id.includes("dall-e") ? "OpenAI DALL·E" : "OpenAI GPT Image";
    result.params = { ...result.params, quality: "auto", output_format: "png" };
  } else if (id.includes("qwen") && id.includes("image")) {
    result.label = "Qwen Image"; result.params = { ...result.params, image_size: "1024x1024", batch_size: 1, num_inference_steps: 20, guidance_scale: 7.5 };
  } else if (id.includes("seedream")) {
    result.label = "Seedream";
    result.params = protocol === "RunningHub" ? { width: 2048, height: 2048, maxImages: 1, sequentialImageGeneration: "disabled" } : { aspect_ratio: "1:1", resolution: "1k", output_format: "jpeg", prompt_optimization_mode: "fast" };
  } else if (id.includes("flux")) {
    result.label = "FLUX"; result.params = { ...result.params, width: 1024, height: 1024, num_inference_steps: 28, guidance_scale: 3.5, seed: -1 };
  } else if (id.includes("stable-diffusion") || id.includes("sdxl") || id.includes("stability")) {
    result.label = "Stable Diffusion"; result.params = { ...result.params, width: 1024, height: 1024, steps: 30, cfg_scale: 7, seed: -1 };
  } else if (id.includes("imagen") || id.includes("gemini") && id.includes("image")) {
    result.label = "Google Imagen / Gemini Image"; result.params = { aspect_ratio: "1:1", output_format: "png", number_of_images: 1 };
  } else if (protocol === "WaveSpeed") {
    result.label = "WaveSpeed 通用模型"; result.params = { output_format: "jpeg" };
  } else if (protocol === "RunningHub") {
    result.label = "RunningHub 自定义工作流"; result.params = {};
  }
  return result;
}
