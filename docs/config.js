// Conexión del tablero con Supabase (Project Settings → API).
//
// La clave "anon public" está pensada para ir en el navegador: lo que impide
// que un usuario vea lotes ajenos son las políticas de Row Level Security de
// supabase/schema.sql, no el secreto de esta clave.
//
// NUNCA poner acá la clave service_role: esa saltea todas las políticas.
window.AGROBOT = {
  supabaseUrl: "",          // https://xxxxxxxx.supabase.co
  supabaseAnonKey: "",      // eyJhbGciOi... (anon public)
  botTelegram: "",          // usuario del bot, sin @, para el enlace de vinculación
};
