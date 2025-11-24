using Microsoft.IdentityModel.Tokens;
using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text;

namespace PaymentProcessor.Services
{
    // Demo-only: generate an HMAC-signed JWT for the API Gateway Lambda Authorizer demo.
    // Do NOT use this in production; integrate with your real auth provider.
    public static class DemoJwt
    {
        public static string GenerateDemoJwt(string userId, string secret)
        {
            var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));
            var creds = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);
            var claims = new[] { new Claim(JwtRegisteredClaimNames.Sub, userId) };
            var token = new JwtSecurityToken(
                issuer: "demo",
                audience: "billgo-demo",
                claims: claims,
                expires: DateTime.UtcNow.AddHours(1),
                signingCredentials: creds);
            return new JwtSecurityTokenHandler().WriteToken(token);
        }
    }
}
