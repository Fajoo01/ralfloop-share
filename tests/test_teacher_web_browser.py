"""Real Chrome mobile smoke; run against an explicitly seeded local demo server."""
import os
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("TEACHER_WEB_BROWSER") != "1", reason="Requires local demo web and Chrome")
def test_student_mobile_browser_flow_and_renderers():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    options=webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=390,844")
    options.set_capability("goog:loggingPrefs", {"browser":"ALL"})
    driver_path=os.environ.get("TEACHER_CHROMEDRIVER", str(Path.home() / ".cache/selenium/chromedriver/linux64/147.0.7727.117/chromedriver"))
    browser=webdriver.Chrome(service=Service(driver_path),options=options)
    wait=WebDriverWait(browser,15)
    base=os.environ.get("TEACHER_WEB_BASE_URL", "http://127.0.0.1:19139").rstrip("/")
    screenshots=Path.home() / ".local/state/ralf-teacher-web-demo/evidence"
    screenshots.mkdir(parents=True,exist_ok=True)
    def visit(path):
        browser.get(base+path)
        wait.until(lambda d: "Un momento…" not in d.find_element(By.ID,"main").text and "Preparo il tuo spazio" not in d.find_element(By.ID,"main").text)
    def click(text):
        wait.until(EC.element_to_be_clickable((By.XPATH,f"//button[normalize-space()='{text}']"))).click()
    def create(kind):
        out=browser.execute_async_script("""const done=arguments[arguments.length-1];fetch('/api/activities',{method:'POST',headers:{'Content-Type':'application/json','X-Teacher-Request':'1'},body:JSON.stringify({topic:'fractions',activity_type:arguments[0]})}).then(r=>r.json()).then(done);""",kind)
        assert out.get("activity_id"), out
        visit('/activity?id='+out['activity_id'])
        return out
    try:
        browser.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",{"width":390,"height":844,"deviceScaleFactor":1,"mobile":True})
        visit('/login')
        assert browser.find_element(By.CSS_SELECTOR,'.welcome-logo').get_attribute('naturalWidth') == '640'
        browser.save_screenshot(str(screenshots/'login-mobile.png'))
        browser.find_element(By.ID,'field-card').send_keys('DEMO-MIDDLE')
        browser.find_element(By.ID,'field-credential').send_keys('StudioDemo!2026')
        click('Entra')
        wait.until(EC.text_to_be_present_in_element((By.ID,'main'),'Ciao, Marco Demo'))
        assert browser.execute_script('return document.documentElement.scrollWidth <= window.innerWidth')
        browser.save_screenshot(str(screenshots/'home-mobile.png'))
        a=create('matching')
        for i,value in enumerate(['2/4','2/6','6/8']): Select(browser.find_element(By.ID,f'pair-{i}')).select_by_visible_text(value)
        click('Invia risposta')
        wait.until(EC.text_to_be_present_in_element((By.ID,'main'),'Risposta corretta.'))
        assert 'XP' in browser.find_element(By.ID,'main').text
        browser.save_screenshot(str(screenshots/'activity-mobile.png'))
        visit('/progress')
        assert 'Frazioni equivalenti' in browser.find_element(By.ID,'main').text
        for kind in ('multiple_choice','true_false','free_answer','grouping','ordering','fill_blank','flashcards','memory','definition_match','sequence','timed_challenge','guided_exercise'):
            a=create(kind)
            assert browser.find_element(By.CSS_SELECTOR,'.activity h1').text
            assert browser.execute_script('return document.documentElement.scrollWidth <= window.innerWidth'),kind
        create('simulation')
        prediction=browser.find_element(By.ID,'field-prediction')
        if prediction.is_enabled():
            prediction.send_keys('La frazione rappresenta la metà.')
            click('Salva previsione')
        wait.until(EC.element_to_be_clickable((By.ID,'field-numerator')))
        browser.execute_script("document.querySelector('#field-numerator').value=2; document.querySelector('#field-denominator').value=4;")
        click('Osserva il risultato')
        wait.until(EC.text_to_be_present_in_element((By.ID,'main'),'2/4 = 0.5'))
        browser.save_screenshot(str(screenshots/'simulation-mobile.png'))
        visit('/books')
        assert 'Quaderno demo' in browser.find_element(By.ID,'main').text
        click('Ascolta')
        wait.until(EC.text_to_be_present_in_element((By.ID,'main'),'Riproduci'))
        assert 'Peppone' in browser.find_element(By.ID,'main').text
        assert 'lettura browser' in browser.find_element(By.ID,'main').text
        # Explicitly surface a failed API request without exposing response internals.
        visit('/study')
        browser.execute_script("window.fetch=async()=>({ok:false,status:503,json:async()=>({error:'Il tutor non è disponibile. Riprova.'})});")
        click('Percorso consigliato')
        wait.until(EC.text_to_be_present_in_element((By.ID,'notice'),'Il tutor non è disponibile'))
        errors=[e for e in browser.get_log('browser') if e['level']=='SEVERE' and 'favicon' not in e['message']]
        assert errors == [], errors
    finally:
        browser.quit()
