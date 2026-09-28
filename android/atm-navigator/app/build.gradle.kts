plugins {
    id("com.android.application")
}

android {
    namespace = "it.tiremminnanz.navigatore"
    compileSdk = 36

    defaultConfig {
        applicationId = "it.tiremminnanz.navigatore"
        minSdk = 26
        targetSdk = 36
        versionCode = 6
        versionName = "0.4.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("org.maplibre.gl:android-sdk-opengl:13.5.0")
}
