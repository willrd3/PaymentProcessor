using Microsoft.AspNetCore.Http;
using System;

namespace PaymentProcessor.Models
{
    public class HomeIndexViewModel
    {
        // Uploaded file (bound from the form)
        public IFormFile? File { get; set; }

        // Document type selected by the user (e.g., invoice, bill)
        public string? DocumentType { get; set; }

        // Correlation id for this upload (generated server-side)
        public string? CorrelationId { get; set; }

        // Optional user id (populated server-side)
        public string? UserId { get; set; }

        // Any error message or status to display on the Index view
        public string? ErrorMessage { get; set; }

        // Raw response or status returned from the ingestion API
        public string? ResponseMessage { get; set; }

        // Timestamp when the form was submitted
        public DateTime? SubmittedAt { get; set; }
    }
}
