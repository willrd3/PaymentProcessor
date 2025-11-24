using Microsoft.AspNetCore.Mvc;
using PaymentProcessor.Models;
using PaymentProcessor.Services;
using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace PaymentProcessor.Controllers
{
    public class HomeController : Controller
    {
        private readonly IHttpClientFactory _httpFactory;
        private readonly IConfiguration _config;

        public HomeController(IHttpClientFactory httpFactory, IConfiguration config)
        {
            _httpFactory = httpFactory;
            _config = config;
        }

        // Explicit GET handler for the home page
        // Returns a strongly-typed view model for Index so we can pass ErrorMessage
        [HttpGet]
        public IActionResult Index()
        {
            var vm = new HomeIndexViewModel();
            return View(vm);
        }

        // Explicit GET handler for privacy page
        [HttpGet]
        public IActionResult Privacy()
        {
            return View();
        }

        // POST /Home/Upload
        // Accepts HomeIndexViewModel (IFormFile File, DocumentType etc.)
        [HttpPost]
        [ValidateAntiForgeryToken]
        public async Task<IActionResult> Upload(HomeIndexViewModel model)
        {
            // Ensure model is not null
            model ??= new HomeIndexViewModel();

            // Basic validation: ensure a file was provided
            if (model.File == null || model.File.Length == 0)
            {
                model.ErrorMessage = "file is required";
                return View("Index", model);
            }

            // Read the whole file into memory. This is acceptable for small demo files but
            // in production you'd stream to S3 or enforce strict size limits to avoid high memory usage.
            byte[] bytes;
            using (var ms = new MemoryStream())
            {
                await model.File.CopyToAsync(ms);
                bytes = ms.ToArray();
            }

            // Convert binary to base64 so we can embed the PDF in a JSON payload for the demo pipeline.
            var b64 = Convert.ToBase64String(bytes);

            // Create a correlation id so the frontend can poll for results later.
            model.CorrelationId = Guid.NewGuid().ToString();
            model.UserId = User?.Identity?.Name ?? "demo";
            model.SubmittedAt = DateTime.UtcNow;

            // Build the payload the ingest Lambda expects.
            var payload = new
            {
                correlationId = model.CorrelationId,
                userId = model.UserId,
                fileName = model.File.FileName,
                documentBase64 = b64,
                documentType = string.IsNullOrWhiteSpace(model.DocumentType) ? "invoice" : model.DocumentType
            };

            // Read API Gateway URL from configuration. If not configured, set ErrorMessage on model and return view.
            var apiUrl = _config["ApiGateway:ProcessUrl"];
            if (string.IsNullOrEmpty(apiUrl))
            {
                model.ErrorMessage = "ApiGateway:ProcessUrl is not configured. Configure ApiGateway:ProcessUrl to enable ingestion.";
                return View("Index", model);
            }

            // Use named HttpClient for API Gateway
            var client = _httpFactory.CreateClient("ApiGateway");

            // No authentication required for this API Gateway; do not add Authorization header.

            var json = JsonSerializer.Serialize(payload);
            var content = new StringContent(json, Encoding.UTF8, "application/json");

            try
            {
                // POST to {apiUrl}/processDocument
                var resp = await client.PostAsync(apiUrl.TrimEnd('/') + "/", content);
                var respBody = await resp.Content.ReadAsStringAsync();

                // Populate response message on the model and return the Index view with the model so the message persists.
                model.ResponseMessage = respBody;
                return View("Index", model);
            }
            catch (TaskCanceledException tce)
            {
                model.ErrorMessage = "Request to API Gateway timed out.";
                return View("Index", model);
            }
            catch (Exception ex)
            {
                model.ErrorMessage = $"Failed to call ingestion API: {ex.Message}";
                return View("Index", model);
            }
        }
    }
}
