class MainController < ApplicationController
  # Pretend Rails controller — exists to give the `app/` subtree a
  # representative file so the display-path trimming rule can be
  # exercised by the test-folder fixture. When dragged onto the .app
  # via an absolute path, this should display as
  # `test-folder/app/controllers/main_controller.rb` (NOT the full
  # /Users/.../test-folder/... absolute path).
  def index
    @greeting = "hello from the app/ subtree"
  end
end
