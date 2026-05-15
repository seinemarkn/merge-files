class User < ApplicationRecord
  # Pretend Rails model. The `app/models/` subdirectory is the second
  # canonical Rails location after `app/controllers/`; both should display
  # with `app/` in the trimmed banner path.
  validates :email, presence: true
end
